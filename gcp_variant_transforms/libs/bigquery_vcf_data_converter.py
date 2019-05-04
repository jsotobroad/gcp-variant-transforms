# Copyright 2018 Google Inc.  All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Handles the conversion between BigQuery row and variant."""

from __future__ import absolute_import
from __future__ import division

import copy
import json
from typing import Any, Dict, List  # pylint: disable=unused-import

from gcp_variant_transforms.beam_io import vcfio
from gcp_variant_transforms.libs.annotation import annotation_parser
from gcp_variant_transforms.libs import bigquery_schema_descriptor  # pylint: disable=unused-import
from gcp_variant_transforms.libs import bigquery_sanitizer
from gcp_variant_transforms.libs import bigquery_util
from gcp_variant_transforms.libs import processed_variant  # pylint: disable=unused-import
from gcp_variant_transforms.libs import vcf_field_conflict_resolver  # pylint: disable=unused-import


_BigQuerySchemaSanitizer = bigquery_sanitizer.SchemaSanitizer

# Reserved constants for column names in the BigQuery schema.
RESERVED_BQ_COLUMNS = [bigquery_util.ColumnKeyConstants.REFERENCE_NAME,
                       bigquery_util.ColumnKeyConstants.START_POSITION,
                       bigquery_util.ColumnKeyConstants.END_POSITION,
                       bigquery_util.ColumnKeyConstants.REFERENCE_BASES,
                       bigquery_util.ColumnKeyConstants.ALTERNATE_BASES,
                       bigquery_util.ColumnKeyConstants.NAMES,
                       bigquery_util.ColumnKeyConstants.QUALITY,
                       bigquery_util.ColumnKeyConstants.FILTER,
                       bigquery_util.ColumnKeyConstants.CALLS]

RESERVED_VARIANT_CALL_COLUMNS = [
    bigquery_util.ColumnKeyConstants.CALLS_NAME,
    bigquery_util.ColumnKeyConstants.CALLS_GENOTYPE,
    bigquery_util.ColumnKeyConstants.CALLS_PHASESET
]

# Constants for calculating row size. This is needed because the maximum size of
# a BigQuery row is 100MB. See
# https://cloud.google.com/bigquery/quotas#import for details.
# We set it to 90MB to leave some room for error as our row estimate is based
# on sampling rather than exact byte size.
_MAX_BIGQUERY_ROW_SIZE_BYTES = 90 * 1024 * 1024
# Number of calls to sample for BigQuery row size estimation.
_NUM_CALL_SAMPLES = 5
# Row size estimation based on sampling calls can be expensive and unnecessary
# when there are not enough calls, so it's only enabled when there are at least
# this many calls.
_MIN_NUM_CALLS_FOR_ROW_SIZE_ESTIMATION = 100

VARIANT_STATE = 'v'
STAR_STATE = 's'
MISSING_STATE = 'n'

PET_SAMPLE_COLUMN = 'sample'
PET_POSITION_COLUMN = 'position'
PET_STATE_COLUMN = 'state'

class BigQueryRowGenerator(object):
  """Class to generate BigQuery row from a variant."""

  def __init__(
      self,
      schema_descriptor,  # type: bigquery_schema_descriptor.SchemaDescriptor
      conflict_resolver=None,
      # type: vcf_field_conflict_resolver.ConflictResolver
      null_numeric_value_replacement=None  # type: int
      ):
    # type: (...) -> None
    self._schema_descriptor = schema_descriptor
    self._conflict_resolver = conflict_resolver
    self._bigquery_field_sanitizer = bigquery_sanitizer.FieldSanitizer(
        null_numeric_value_replacement)

  def get_rows(self,
               variant,
               allow_incompatible_records=False,
               omit_empty_sample_calls=False,
               write_to_pet=False):
    # type: (processed_variant.ProcessedVariant, bool, bool) -> Dict
    """Yields BigQuery rows according to the schema from the given variant.

    There is a 100MB limit for each BigQuery row, which can be exceeded by
    having a large number of calls. This method may split up a row into
    multiple rows if it exceeds the limit.

    Args:
      variant: Variant to convert to a row.
      allow_incompatible_records: If true, field values are casted to Bigquery
        schema if there is a mismatch.
      omit_empty_sample_calls: If true, samples that don't have a given
        call will be omitted.
      write_to_pet: should i write to PET or VET
    Yields:
      A dict representing a BigQuery row from the given variant. The row may
      have a subset of the calls if it exceeds the maximum allowed BigQuery
      row size.
    Raises:
      ValueError: If variant data is inconsistent or invalid.
    """
    sample_name = variant.calls[0].name
    start = variant.start

    if len(variant.alternate_data_list) > 1 and not write_to_pet:
        base_row = self._get_base_row_from_variant(
            variant, allow_incompatible_records)
        call_limit_per_row = self._get_call_limit_per_row(variant)
        if call_limit_per_row < len(variant.calls):
          # Keep base_row intact if we need to split rows.
          row = copy.deepcopy(base_row)
        else:
          row = base_row

        call_record_schema_descriptor = (
            self._schema_descriptor.get_record_schema_descriptor(
                bigquery_util.ColumnKeyConstants.CALLS))
        num_calls_in_row = 0
        for call in variant.calls:
          call_record, empty = self._get_call_record(
              call, call_record_schema_descriptor, allow_incompatible_records)
          if omit_empty_sample_calls and empty:
            continue
          num_calls_in_row += 1
          if num_calls_in_row > call_limit_per_row:
            yield row
            num_calls_in_row = 1
            row = copy.deepcopy(base_row)
          row[bigquery_util.ColumnKeyConstants.CALLS].append(call_record)
        yield row

    if write_to_pet:
        if len(variant.alternate_data_list) > 1:
            row = {}
            row[PET_POSITION_COLUMN] = variant.start
            row[PET_SAMPLE_COLUMN] = sample_name
            row[PET_STATE_COLUMN] = VARIANT_STATE
            yield row

            block_size = variant.end - start + 1
            for offset in range(1, block_size):
                row = {}
                row[PET_POSITION_COLUMN] = start + offset
                row[PET_SAMPLE_COLUMN] = sample_name
                row[PET_STATE_COLUMN] = STAR_STATE
                yield row

        if len(variant.alternate_data_list) == 1:
            gq = variant.calls[0].info['GQ']
            if gq < 10:
                non_ref_state = "0"
            elif gq < 20:
                non_ref_state = "10"
            elif gq < 30:
                non_ref_state = "20"
            elif gq < 40:
                non_ref_state = "30"
            elif gq < 50:
                non_ref_state = "40"
            elif gq < 60:
                non_ref_state = "50"
            else:
                non_ref_state = "60"

            if non_ref_state:
                block_size = variant.end - start + 1
                for offset in range(0, block_size):
                    row = {}
                    row[PET_POSITION_COLUMN] = start + offset
                    row[PET_SAMPLE_COLUMN] = sample_name
                    row[PET_STATE_COLUMN] = non_ref_state
                    yield row



  def _get_call_record(
      self,
      call,  # type: vcfio.VariantCall
      call_record_schema_descriptor,
      # type: bigquery_schema_descriptor.SchemaDescriptor
      allow_incompatible_records,  # type: bool
      ):
    # type: (...) -> (Dict[str, Any], bool)
    """A helper method for ``get_rows`` to get a call as JSON.

    Args:
      call: Variant call to convert.
      call_record_schema_descriptor: Descriptor for the BigQuery schema of
        call record.

    Returns:
      BigQuery call value (dict).
    """
    call_record = {
        bigquery_util.ColumnKeyConstants.CALLS_NAME:
            self._bigquery_field_sanitizer.get_sanitized_field(call.name),
        bigquery_util.ColumnKeyConstants.CALLS_PHASESET: call.phaseset,
        bigquery_util.ColumnKeyConstants.CALLS_GENOTYPE: call.genotype or []
    }
    is_empty = (not call.genotype or
                set(call.genotype) == set((vcfio.MISSING_GENOTYPE_VALUE,)))
    for key, data in call.info.iteritems():
      if data is not None:
        field_name, field_data = self._get_bigquery_field_entry(
            key, data, call_record_schema_descriptor,
            allow_incompatible_records)
        call_record[field_name] = field_data
        is_empty = is_empty and self._is_empty_field(field_data)
    return call_record, is_empty

  def _get_base_row_from_variant(self, variant, allow_incompatible_records):
    # type: (processed_variant.ProcessedVariant, bool) -> Dict[str, Any]
    row = {
        bigquery_util.ColumnKeyConstants.REFERENCE_NAME: variant.reference_name,
        bigquery_util.ColumnKeyConstants.START_POSITION: variant.start,
        bigquery_util.ColumnKeyConstants.END_POSITION: variant.end,
        bigquery_util.ColumnKeyConstants.REFERENCE_BASES:
        variant.reference_bases
    }  # type: Dict[str, Any]
    if variant.names:
      row[bigquery_util.ColumnKeyConstants.NAMES] = (
          self._bigquery_field_sanitizer.get_sanitized_field(variant.names))
    if variant.quality is not None:
      row[bigquery_util.ColumnKeyConstants.QUALITY] = variant.quality
    if variant.filters:
      row[bigquery_util.ColumnKeyConstants.FILTER] = (
          self._bigquery_field_sanitizer.get_sanitized_field(variant.filters))
    # Add alternate bases.
    row[bigquery_util.ColumnKeyConstants.ALTERNATE_BASES] = []
    for alt in variant.alternate_data_list:
      alt_record = {bigquery_util.ColumnKeyConstants.ALTERNATE_BASES_ALT:
                    alt.alternate_bases}
      for key, data in alt.info.iteritems():
        alt_record[_BigQuerySchemaSanitizer.get_sanitized_field_name(key)] = (
            data if key in alt.annotation_field_names else
            self._bigquery_field_sanitizer.get_sanitized_field(data))
      row[bigquery_util.ColumnKeyConstants.ALTERNATE_BASES].append(alt_record)
    # Add info.
    for key, data in variant.non_alt_info.iteritems():
      if data is not None:
        field_name, field_data = self._get_bigquery_field_entry(
            key, data, self._schema_descriptor, allow_incompatible_records)
        row[field_name] = field_data

    # Set calls to empty for now (will be filled later).
    row[bigquery_util.ColumnKeyConstants.CALLS] = []
    return row

  def _get_bigquery_field_entry(
      self,
      key,  # type: str
      data,  # type: Union[Any, List[Any]]
      schema_descriptor,  # type: bigquery_schema_descriptor.SchemaDescriptor
      allow_incompatible_records,  # type: bool
  ):
    # type: (...) -> (str, Any)
    if data is None:
      return None, None
    field_name = _BigQuerySchemaSanitizer.get_sanitized_field_name(key)
    if not schema_descriptor.has_simple_field(field_name):
      raise ValueError('BigQuery schema has no such field: {}.\n'
                       'This can happen if the field is not defined in '
                       'the VCF headers, or is not inferred automatically. '
                       'Retry pipeline with --infer_headers.'
                       .format(field_name))
    sanitized_field_data = self._bigquery_field_sanitizer.get_sanitized_field(
        data)
    field_schema = schema_descriptor.get_field_descriptor(field_name)
    field_data, is_compatible = self._check_and_resolve_schema_compatibility(
        field_schema, sanitized_field_data)
    if is_compatible or allow_incompatible_records:
      return field_name, field_data
    else:
      raise ValueError('Value and schema do not match for field {}. '
                       'Value: {} Schema: {}.'.format(
                           field_name, sanitized_field_data, field_schema))

  def _check_and_resolve_schema_compatibility(self, field_schema, field_data):
    resolved_field_data = self._conflict_resolver.resolve_schema_conflict(
        field_schema, field_data)
    return resolved_field_data, resolved_field_data == field_data

  def _is_empty_field(self, value):
    return (value in (vcfio.MISSING_FIELD_VALUE, [vcfio.MISSING_FIELD_VALUE]) or
            (not value and value != 0))

  def _get_call_limit_per_row(self, variant):
    # type: (processed_variant.ProcessedVariant) -> int
    """Returns an estimate of maximum number of calls per BigQuery row.

    This method works by sampling `variant.calls` and getting an estimate based
    on the serialized JSON value.
    """
    # Assume that the non-call part of the variant is negligible compared to the
    # call part.
    num_calls = len(variant.calls)
    if num_calls < _MIN_NUM_CALLS_FOR_ROW_SIZE_ESTIMATION:
      return num_calls
    average_call_size_bytes = self._get_average_call_size_bytes(variant.calls)
    if average_call_size_bytes * num_calls <= _MAX_BIGQUERY_ROW_SIZE_BYTES:
      return num_calls
    else:
      return _MAX_BIGQUERY_ROW_SIZE_BYTES // average_call_size_bytes

  def _get_average_call_size_bytes(self, calls):
    # type: (List[vcfio.VariantCall]) -> int
    """Returns the average call size based on sampling."""
    sum_sampled_call_size_bytes = 0
    num_sampled_calls = 0
    call_record_schema_descriptor = (
        self._schema_descriptor.get_record_schema_descriptor(
            bigquery_util.ColumnKeyConstants.CALLS))
    for call in calls[::len(calls) // _NUM_CALL_SAMPLES]:
      call_record, _ = self._get_call_record(
          call, call_record_schema_descriptor, allow_incompatible_records=True)
      sum_sampled_call_size_bytes += self._get_json_object_size(call_record)
      num_sampled_calls += 1
    return sum_sampled_call_size_bytes // num_sampled_calls

  def _get_json_object_size(self, obj):
    return len(json.dumps(obj))


class VariantGenerator(object):
  """Class to generate variant from one BigQuery row."""

  def __init__(self, annotation_id_to_annotation_names=None):
    # type: (Dict[str, List[str]]) -> None
    """Initializes an object of `VariantGenerator`.

    Args:
      annotation_id_to_annotation_names: A map where the key is the annotation
        id (e.g., `CSQ`) and the value is a list of annotation names (e.g.,
        ['allele', 'Consequence', 'IMPACT', 'SYMBOL']). The annotation str
        (e.g., 'A|upstream_gene_variant|MODIFIER|PSMF1|||||') is reconstructed
        in the same order as the annotation names.
    """
    self._annotation_str_builder = annotation_parser.AnnotationStrBuilder(
        annotation_id_to_annotation_names)

  def convert_bq_row_to_variant(self, row):
    """Converts one BigQuery row to `Variant`."""
    # type: (Dict[str, Any]) -> vcfio.Variant
    return vcfio.Variant(
        reference_name=row[bigquery_util.ColumnKeyConstants.REFERENCE_NAME],
        start=row[bigquery_util.ColumnKeyConstants.START_POSITION],
        end=row[bigquery_util.ColumnKeyConstants.END_POSITION],
        reference_bases=row[bigquery_util.ColumnKeyConstants.REFERENCE_BASES],
        alternate_bases=self._get_alternate_bases(
            row[bigquery_util.ColumnKeyConstants.ALTERNATE_BASES]),
        names=row[bigquery_util.ColumnKeyConstants.NAMES],
        quality=row[bigquery_util.ColumnKeyConstants.QUALITY],
        filters=row[bigquery_util.ColumnKeyConstants.FILTER],
        info=self._get_variant_info(row),
        calls=self._get_variant_calls(
            row[bigquery_util.ColumnKeyConstants.CALLS])
    )

  def _get_alternate_bases(self, alternate_base_records):
    # type: (List[Dict[str, Any]]) -> List[str]
    return [record[bigquery_util.ColumnKeyConstants.ALTERNATE_BASES_ALT]
            for record in alternate_base_records]

  def _get_variant_info(self, row):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    info = {}
    for key, value in row.iteritems():
      if key not in RESERVED_BQ_COLUMNS and not self._is_null_or_empty(value):
        info.update({key: value})
    for alt_base in row[bigquery_util.ColumnKeyConstants.ALTERNATE_BASES]:
      for key, value in alt_base.iteritems():
        if (key != bigquery_util.ColumnKeyConstants.ALTERNATE_BASES_ALT and
            not self._is_null_or_empty(value)):
          if key not in info:
            info[key] = []
          if self._annotation_str_builder.is_valid_annotation_id(key):
            info[key].extend(
                self._annotation_str_builder.reconstruct_annotation_str(
                    key, value))
          else:
            info[key].append(value)
    return info

  def _get_variant_calls(self, variant_call_records):
    # type: (List[Dict[str, Any]]) -> List[vcfio.VariantCall]
    variant_calls = []
    for call_record in variant_call_records:
      info = {}
      for key, value in call_record.iteritems():
        if (key not in RESERVED_VARIANT_CALL_COLUMNS and
            not self._is_null_or_empty(value)):
          info.update({key: value})
      variant_call = vcfio.VariantCall(
          name=call_record[bigquery_util.ColumnKeyConstants.CALLS_NAME],
          genotype=call_record[bigquery_util.ColumnKeyConstants.CALLS_GENOTYPE],
          phaseset=call_record[bigquery_util.ColumnKeyConstants.CALLS_PHASESET],
          info=info)
      variant_calls.append(variant_call)
    return variant_calls

  def _is_null_or_empty(self, value):
    # type: (Any) -> bool
    if value is None:
      return True
    if isinstance(value, list) and not value:
      return True
    return False
