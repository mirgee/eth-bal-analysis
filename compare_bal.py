import os
import sys
import json
import requests
from pathlib import Path
from deepdiff import DeepDiff

sys.path.append(str(Path(__file__).resolve().parent / "src"))

from bal_builder import fetch_block_trace, process_storage_changes, process_balance_changes
from bal_builder import process_code_changes, process_nonce_changes, collect_touched_addresses
from bal_builder import BALBuilder, sort_block_access_list
from bal_builder import extract_reads_from_block
from BALs import BlockAccessList

ALCHEMY_RPC_URL = ""
BESU_RPC_URL = "http://localhost:8545"
BLOCK_NUMBER = 22774213

def convert_bal_to_json(bal: BlockAccessList) -> dict:
    """Convert SSZ BAL to human-readable JSON."""
    def b64(b): return '0x' + b.hex().rjust(64, '0')

    def b(b): return '0x' + b.hex()

    def storage_changes(sc):
        return {
            "slot": b(sc.slot),
            "changes": [
                {"txIndex": c.tx_index, "newValue": b(c.new_value)}
                for c in sc.changes
            ]
        }

    def account(acct):
        return {
            "address": b(acct.address),
            "storageChanges": [storage_changes(sc) for sc in acct.storage_changes],
            "storageReads": [b(sr.slot) for sr in acct.storage_reads],
            "balanceChanges": [
                {"txIndex": bc.tx_index, "postBalance": b64(bc.post_balance)}
                for bc in acct.balance_changes
            ],
            "nonceChanges": [
                {"txIndex": nc.tx_index, "newNonce": nc.new_nonce}
                for nc in acct.nonce_changes
            ],
            "codeChanges": [
                {"txIndex": cc.tx_index, "newCode": b(cc.new_code)}
                for cc in acct.code_changes
            ]
        }

    return {
        "accountChanges": [account(acct) for acct in bal.account_changes]
    }


def get_reference_bal_for_block() -> dict:
    trace = fetch_block_trace(BLOCK_NUMBER, ALCHEMY_RPC_URL)
    reads = extract_reads_from_block(BLOCK_NUMBER, ALCHEMY_RPC_URL)
    builder = BALBuilder()
    touched = collect_touched_addresses(trace)
    process_storage_changes(trace, additional_reads=reads, ignore_reads=False, builder=builder)
    process_balance_changes(trace, builder=builder, touched_addresses=touched)
    process_code_changes(trace, builder)
    process_nonce_changes(trace, builder)
    for addr in touched:
        builder.add_touched_account(bytes.fromhex(addr[2:]) if addr.startswith("0x") else bytes.fromhex(addr))
    bal = builder.build(ignore_reads=False)
    sorted_bal = sort_block_access_list(bal)
    return convert_bal_to_json(sorted_bal)


def get_block_transactions() -> list[dict]:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBlockByNumber",
        "params": [hex(BLOCK_NUMBER), True],
        "id": 1
    }

    resp = requests.post(BESU_RPC_URL, json=payload).json()
    if "error" in resp:
        raise Exception(f"Error fetching block: {resp['error']}")
    
    txs = resp["result"]["transactions"]
    formatted = []
    for tx in txs:
        formatted.append({
            "from": tx["from"],
            "to": tx.get("to"),
            "gas": hex(int(tx["gas"], 16)),
            "gasPrice": hex(int(tx.get("gasPrice", "0x0"), 16)),
            "value": hex(int(tx.get("value", "0x0"), 16)),
            "input": tx.get("input", "0x")
        })
    
    return formatted


def simulate_transactions(transactions: list[dict]) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_simulateV1",
        "params": [
            {
                "blockStateCalls": [
                    {"calls": transactions}
                ],
                "validation": True,
                "traceTransfers": False
            },
            str(BLOCK_NUMBER - 1)
        ],
        "id": 1
    }

    resp = requests.post(BESU_RPC_URL, json=payload).json()
    if "error" in resp:
        raise Exception(f"Simulation failed: {resp['error']}")
    
    # Assumes result is a list with one item
    return resp["result"][0]["blockAccessList"]


def extract_storage_changes_map(bal_json: dict) -> dict:
    result = {}
    for acct in bal_json.get("accountChanges", []):
        addr = acct["address"].lower()
        slot_map = {}
        for slot_entry in acct.get("storageChanges", []):
            slot = slot_entry["slot"].lower()
            changes = tuple(
                (entry["txIndex"], entry["newValue"].lower())
                for entry in slot_entry["changes"]
            )
            slot_map[slot] = changes
        result[addr] = slot_map
    return result


def dump_bal_jsons(ref_bal: dict, besu_bal: dict, output_dir: Path = None):
    if output_dir is None:
        output_dir = Path(__file__).resolve().parent / "cmp"
    output_dir.mkdir(parents=True, exist_ok=True)

    ref_path = output_dir / f"{BLOCK_NUMBER}_ref_bal.json"
    besu_path = output_dir / f"{BLOCK_NUMBER}_besu_bal.json"

    with open(ref_path, "w") as f:
        json.dump(ref_bal, f, indent=2)
    with open(besu_path, "w") as f:
        json.dump(besu_bal, f, indent=2)


def dump_diff_json(diff: dict, output_dir: Path = None):
    if output_dir is None:
        output_dir = Path(__file__).resolve().parent / "cmp"
    output_dir.mkdir(parents=True, exist_ok=True)

    diff_path = output_dir / f"{BLOCK_NUMBER}_diff.json"

    with open(diff_path, "w") as f:
        f.write(diff.to_json(indent=2))


def remove_zero_address(json_data):
    ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

    json_data["accountChanges"] = [
        acct for acct in json_data["accountChanges"] if acct["address"] != ZERO_ADDRESS
    ]

    return json_data


def index_account_changes(account_changes):
    return {
        entry['address']: entry
        for entry in account_changes
    }

if __name__ == "__main__":
    ref_bal = get_reference_bal_for_block()
    txs = get_block_transactions()
    besu_bal = simulate_transactions(txs)
    besu_bal = remove_zero_address(besu_bal)
    ref_bal = index_account_changes(ref_bal['accountChanges'])
    besu_bal = index_account_changes(besu_bal['accountChanges'])

    dump_bal_jsons(ref_bal, besu_bal)

    cmp_dir = Path(__file__).resolve().parent / "cmp"
    cmp_dir.mkdir(exist_ok=True)

    diff = DeepDiff(ref_bal, besu_bal, verbose_level=0, ignore_order=True)
    dump_diff_json(diff)

