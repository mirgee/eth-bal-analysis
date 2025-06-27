from src.bal_builder import (
    fetch_block_trace,
    extract_reads_from_block,
    process_storage_changes,
    process_balance_changes,
    process_code_changes,
    process_nonce_changes,
    sort_block_access_list,
    collect_touched_addresses
)
from src.BALs import BALBuilder, BlockAccessList
from src.helpers import parse_hex_or_zero

import json
import os
from ssz import encode as ssz_encode, Serializable


# Read the RPC URL
rpc_file = "rpc.txt"
with open(rpc_file, "r") as file:
    rpc_url = file.read().strip()


def ssz_to_json(obj):
    if isinstance(obj, Serializable):
        return {
            field_name: ssz_to_json(getattr(obj, field_name))
            for field_name, _ in obj.__class__._meta.fields
        }
    elif isinstance(obj, (list, tuple)):
        return [ssz_to_json(item) for item in obj]
    elif isinstance(obj, bytes):
        return '0x' + obj.hex()
    elif isinstance(obj, int):
        return obj
    else:
        raise TypeError(f"Cannot convert object of type {type(obj)} to JSON")


def build_and_print_mapped_bal(block_number: int, ignore_reads: bool = False):
    print(f"Fetching trace for block {block_number}...")
    trace_result = fetch_block_trace(block_number, rpc_url)
    print(json.dumps(trace_result, indent=2))

    block_reads = None
    if not ignore_reads:
        print("  Fetching additional reads...")
        block_reads = extract_reads_from_block(block_number, rpc_url)

    builder = BALBuilder()

    # Process all components
    touched = collect_touched_addresses(trace_result)
    process_storage_changes(trace_result, additional_reads=block_reads, ignore_reads=ignore_reads, builder=builder)
    process_balance_changes(trace_result, builder=builder, touched_addresses=touched)
    process_code_changes(trace_result, builder)
    process_nonce_changes(trace_result, builder)

    block_obj = builder.build(ignore_reads=ignore_reads)
    sorted_bal = sort_block_access_list(block_obj)

    # Print JSON
    print(json.dumps(ssz_to_json(sorted_bal), indent=2))

    # Optional: encode as SSZ
    encoded = ssz_encode(sorted_bal, sedes=BlockAccessList)
    print(f"\nSSZ-encoded size: {len(encoded)} bytes")


if __name__ == "__main__":
    build_and_print_mapped_bal(22774214, ignore_reads=False)
