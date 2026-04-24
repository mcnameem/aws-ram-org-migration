#!/usr/bin/env python
from __future__ import annotations

import argparse
import boto3
from typing import Any

ram: boto3.client = None


def paginate(method: str, key: str, **kwargs) -> list[dict[str, Any]]:
    results = []
    params = {**kwargs}
    while True:
        resp = getattr(ram, method)(**params)
        results.extend(resp.get(key, []))
        token = resp.get("nextToken")
        if not token:
            break
        params["nextToken"] = token
    return results


def get_associated_sets(share_arn: str) -> dict:
    resources = paginate(
        "list_resources", "resources",
        resourceOwner="SELF", resourceShareArns=[share_arn],
    )
    principals = paginate(
        "list_principals", "principals",
        resourceOwner="SELF", resourceShareArns=[share_arn],
    )
    permissions = paginate(
        "list_resource_share_permissions", "permissions",
        resourceShareArn=share_arn,
    )
    return {
        "resourceArns": {r["arn"] for r in resources},
        "principalIds": {p["id"] for p in principals},
        "permissionArns": {p["arn"] for p in permissions},
    }


def all_associations_active(share_arn: str) -> bool:
    resource_assocs = paginate(
        "get_resource_share_associations", "resourceShareAssociations",
        associationType="RESOURCE", resourceShareArns=[share_arn],
    )
    principal_assocs = paginate(
        "get_resource_share_associations", "resourceShareAssociations",
        associationType="PRINCIPAL", resourceShareArns=[share_arn],
    )
    for a in resource_assocs + principal_assocs:
        if a["status"] != "ASSOCIATED":
            return False
    return True


def find_matching_share(bridge_arn: str, bridge_details: dict, all_shares: list[dict]) -> str | None:
    for share in all_shares:
        if share["resourceShareArn"] == bridge_arn:
            continue
        other = get_associated_sets(share["resourceShareArn"])
        if (bridge_details["resourceArns"] <= other["resourceArns"]
                and bridge_details["principalIds"] <= other["principalIds"]
                and bridge_details["permissionArns"] <= other["permissionArns"]):
            return share["resourceShareArn"]
    return None


def main():
    global ram
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True)
    parser.add_argument("--execute", action="store_true", default=False)
    args = parser.parse_args()

    ram = boto3.client("ram", region_name=args.region)

    if not args.execute:
        print("[DRYRUN] Running in dry-run mode. No shares will be deleted.")

    # Find all shares tagged as bridge shares
    all_shares = paginate("get_resource_shares", "resourceShares", resourceOwner="SELF")
    bridge_shares = []
    for share in all_shares:
        if share.get("status") != "ACTIVE":
            continue
        tags = {t["key"]: t["value"] for t in share.get("tags", [])}
        if tags.get("bridge-share") == "true":
            bridge_shares.append(share)

    print(f"[DEBUG] Found {len(bridge_shares)} bridge shares out of {len(all_shares)} total shares")

    for bridge in bridge_shares:
        bridge_arn = bridge["resourceShareArn"]
        print(f"\n[DEBUG] Processing bridge share: {bridge_arn} ({bridge['name']})")

        bridge_details = get_associated_sets(bridge_arn)
        print(f"[DEBUG]   Resources: {len(bridge_details['resourceArns'])}, Principals: {len(bridge_details['principalIds'])}, Permissions: {len(bridge_details['permissionArns'])}")

        matching_arn = find_matching_share(bridge_arn, bridge_details, all_shares)
        if not matching_arn:
            print(f"[ERROR]   No matching share found with same principals, resources, and permissions. Skipping delete.")
            continue

        print(f"[DEBUG]   Found matching share: {matching_arn}")

        if not all_associations_active(matching_arn):
            print(f"[ERROR]   Matching share {matching_arn} has associations not in ASSOCIATED state. Skipping delete.")
            continue

        print(f"[DEBUG]   All associations in matching share are ASSOCIATED")

        if args.execute:
            ram.delete_resource_share(resourceShareArn=bridge_arn)
            print(f"[DEBUG]   Deleted bridge share: {bridge_arn}")
        else:
            print(f"[DRYRUN]  Would delete bridge share: {bridge_arn}")

    print("\n[DEBUG] Done.")


if __name__ == "__main__":
    main()
