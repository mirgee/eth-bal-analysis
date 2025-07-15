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
from bal_builder import extract_reads_from_block, fetch_block_receipts, fetch_block_info, extract_balance_touches_from_block
from BALs import BlockAccessList

BESU_RPC_URL = "http://localhost:8545"

rpc_file = os.path.join(Path(__file__).parent, "rpc.txt")
with open(rpc_file, "r") as file:
    ALCHEMY_RPC_URL = file.read().strip()

def convert_bal_to_json(bal: BlockAccessList) -> dict:
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
            "storageChanges": [storage_changes(sc) for sc in acct.storage_writes],
            "storageReads": [b(sr) for sr in acct.storage_reads],
            "balanceChanges": [
                {"txIndex": bc.tx_index, "postBalance": b64(bc.post_balance)}
                for bc in acct.balance_changes
            ],
            # "nonceChanges": [
            #     {"txIndex": nc.tx_index, "newNonce": nc.new_nonce}
            #     for nc in acct.nonce_changes
            # ],
            "codeChanges": [
                {"txIndex": cc.tx_index, "newCode": b(cc.new_code)}
                for cc in acct.code_changes
            ]
        }

    return {
        "accountChanges": [account(acct) for acct in bal.account_changes]
    }

def get_reference_bal_for_block(block_number: int) -> dict:
    trace = fetch_block_trace(block_number, ALCHEMY_RPC_URL)
    balance_touches = extract_balance_touches_from_block(block_number, ALCHEMY_RPC_URL)
    receipts = fetch_block_receipts(block_number, ALCHEMY_RPC_URL)
    reverted_tx_indices = set()
    for i, receipt in enumerate(receipts):
        if receipt and receipt.get("status") == "0x0":
            reverted_tx_indices.add(i)
    if reverted_tx_indices:
        print(f"    Found {len(reverted_tx_indices)} reverted transactions: {sorted(reverted_tx_indices)}")
    block_info = None
    if reverted_tx_indices:
        print(f"  Fetching block info for reverted transaction handling...")
        block_info = fetch_block_info(block_number, ALCHEMY_RPC_URL)
    reads = extract_reads_from_block(block_number, ALCHEMY_RPC_URL)
    builder = BALBuilder()
    touched = collect_touched_addresses(trace)
    process_storage_changes(trace, reads, False, builder, reverted_tx_indices)
    process_balance_changes(trace, builder, touched, balance_touches, reverted_tx_indices, block_info, False)
    process_code_changes(trace, builder, reverted_tx_indices)
    process_nonce_changes(trace, builder, reverted_tx_indices)
    for addr in touched:
        builder.add_touched_account(bytes.fromhex(addr[2:]) if addr.startswith("0x") else bytes.fromhex(addr))
    bal = builder.build(ignore_reads=False)
    sorted_bal = sort_block_access_list(bal)
    return convert_bal_to_json(sorted_bal)

def get_block_transactions(block_number: int) -> list[dict]:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBlockByNumber",
        "params": [hex(block_number), True],
        "id": 1
    }

    resp = requests.post(BESU_RPC_URL, json=payload).json()
    if "error" in resp:
        raise Exception(f"Error fetching block: {resp['error']}")
    
    miner = resp["result"]["miner"]
    txs = resp["result"]["transactions"]
    formatted = []
    for tx in txs:
        tx_obj = {
            "from": tx["from"]
        }
        if "to" in tx:
            tx_obj["to"] = tx["to"]
        if "gas" in tx:
            tx_obj["gas"] = tx["gas"]
        if "value" in tx:
            tx_obj["value"] = tx["value"]
        if "input" in tx:
            tx_obj["input"] = tx["input"]
        if "nonce" in tx:
            tx_obj["nonce"] = tx["nonce"]
        if "maxFeePerGas" in tx and "maxPriorityFeePerGas" in tx:
            tx_obj["maxFeePerGas"] = tx["maxFeePerGas"]
            tx_obj["maxPriorityFeePerGas"] = tx["maxPriorityFeePerGas"]
        elif "gasPrice" in tx:
            tx_obj["gasPrice"] = tx["gasPrice"]
        if "accessList" in tx:
            tx_obj["accessList"] = tx["accessList"]
        if "maxFeePerBlobGas" in tx:
            tx_obj["maxFeePerBlobGas"] = tx["maxFeePerBlobGas"]
        if "blobVersionedHashes" in tx:
            tx_obj["blobVersionedHashes"] = tx["blobVersionedHashes"]
        if "authorizationList" in tx:
            tx_obj["authorizationList"] = tx["authorizationList"]
            for t in tx_obj["authorizationList"]:
                if "yParity" in t:
                    t["v"] = t["yParity"]
                    del t["yParity"]

        formatted.append(tx_obj)

    return formatted, miner

def simulate_transactions(block_number: int, transactions: list[dict]) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_simulateV1",
        "params": [
            {
                "blockStateCalls": [
                    {"calls": transactions}
                ],
                "validation": True,
                "traceTransfers": True
            },
            str(block_number - 1)
        ],
        "id": 1
    }

    resp = requests.post(BESU_RPC_URL, json=payload).json()
    if "error" in resp:
        raise Exception(f"Simulation failed: {resp['error']}")
    
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

def dump_bal_jsons(block_number: int, ref_bal: dict, besu_bal: dict, output_dir: Path = None):
    if output_dir is None:
        output_dir = Path(__file__).resolve().parent / "cmp" / str(block_number)
    output_dir.mkdir(parents=True, exist_ok=True)

    ref_path = output_dir / "bal_ref.json"
    besu_path = output_dir / "bal_besu.json"

    with open(ref_path, "w") as f:
        json.dump(ref_bal, f, indent=2)
    with open(besu_path, "w") as f:
        json.dump(besu_bal, f, indent=2)

def dump_diff_json(block_number: int, diff: dict, output_dir: Path = None):
    if output_dir is None:
        output_dir = Path(__file__).resolve().parent / "cmp" / str(block_number)
    output_dir.mkdir(parents=True, exist_ok=True)

    diff_path = output_dir / "diff.json"

    with open(diff_path, "w") as f:
        f.write(diff.to_json(indent=2))

def remove_address(json_data, address):
    json_data["accountChanges"] = [
        acct for acct in json_data["accountChanges"] if acct["address"] != address
    ]
    return json_data

def index_account_changes(account_changes):
    account_changes = account_changes['accountChanges']
    account_changes = {
        entry['address']: entry
        for entry in account_changes
    }
    for address, entry in account_changes.items():
        entry["storageChanges"] = {
            change["slot"]: change
            for change in entry.get("storageChanges", [])
        }
    return account_changes

if __name__ == "__main__":
    start = 22778554
    length = 1
    for block_num in range(start, start + length):
        print(f"Processing block number {block_num}")
        ref_bal = get_reference_bal_for_block(block_num)
        txs, miner = get_block_transactions(block_num)

        besu_bal = simulate_transactions(block_num, txs)

        besu_bal = remove_address(besu_bal, "0x0000000000000000000000000000000000000000")
        besu_bal = remove_address(besu_bal, miner)
        ref_bal = remove_address(ref_bal, "0x0000000000000000000000000000000000000000")
        ref_bal = remove_address(ref_bal, miner)

        ref_bal = index_account_changes(ref_bal)
        besu_bal = index_account_changes(besu_bal)

        dump_bal_jsons(block_num, ref_bal, besu_bal)

        diff = DeepDiff(ref_bal, besu_bal, verbose_level=0, ignore_order=True)
        dump_diff_json(block_num, diff)
