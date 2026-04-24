#!/usr/bin/env python

import argparse
import json
import time
import boto3
from typing import Any

ram: boto3.client = None


def accept_invitation(resource_share_arn: str, account_id: str, role_name: str, execute: bool):
    sts = boto3.client("sts")
    creds = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{role_name}",
        RoleSessionName="accept-ram-invitation",
    )["Credentials"]
    print(f"[DEBUG]   Successfully assumed role {role_name} in account {account_id}")

    if not execute:
        print(f"[DRYRUN]  Would search for and accept invitation for {resource_share_arn} in account {account_id}")
        return

    remote_ram = boto3.client(
        "ram",
        region_name=ram.meta.region_name,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )

    max_attempts = 10
    for attempt in range(1, max_attempts + 1):
        print(f"[DEBUG]   Checking for invitation in account {account_id} (attempt {attempt}/{max_attempts})")
        invitations = remote_ram.get_resource_share_invitations()["resourceShareInvitations"]
        match = [i for i in invitations if i["resourceShareArn"] == resource_share_arn and i["status"] == "PENDING"]
        if match:
            invitation_arn = match[0]["resourceShareInvitationArn"]
            print(f"[DEBUG]   Found invitation {invitation_arn}, accepting...")
            remote_ram.accept_resource_share_invitation(resourceShareInvitationArn=invitation_arn)
            print(f"[DEBUG]   Invitation accepted for account {account_id}")
            return
        time.sleep(5)

    raise TimeoutError(f"Invitation for {resource_share_arn} not found in account {account_id} after {max_attempts} attempts")


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


def get_share_details(resource_share_arn: str) -> dict:
    resources = paginate(
        "get_resource_share_associations",
        "resourceShareAssociations",
        associationType="RESOURCE",
        resourceShareArns=[resource_share_arn],
    )
    principals = paginate(
        "get_resource_share_associations",
        "resourceShareAssociations",
        associationType="PRINCIPAL",
        resourceShareArns=[resource_share_arn],
    )
    permissions = paginate(
        "list_resource_share_permissions",
        "permissions",
        resourceShareArn=resource_share_arn,
    )
    return {
        "resources": resources,
        "principals": principals,
        "permissions": permissions,
    }


def create_duplicate_share(original_arn: str, details: dict) -> str:
    original = ram.get_resource_shares(
        resourceShareArns=[original_arn], resourceOwner="SELF"
    )["resourceShares"][0]

    resource_arns = [r["associatedEntity"] for r in details["resources"]]
    principal_ids = [p["associatedEntity"] for p in details["principals"]]
    permission_arns = [p["arn"] for p in details["permissions"]]

    # create_resource_share supports max 100 resources and 100 principals
    print(f"[DEBUG]   Creating share '{original['name']}-duplicate' with {len(resource_arns)} resources, {len(principal_ids)} principals, {len(permission_arns)} permissions")
    create_resp = ram.create_resource_share(
        name=f"{original['name']}-bridge",
        resourceArns=resource_arns[:100],
        principals=principal_ids[:100],
        permissionArns=permission_arns,
        allowExternalPrincipals=True,
        resourceShareConfiguration={
            "retainSharingOnAccountLeaveOrganization": True
        },
        tags=[
            {"key": "bridge-share", "value": "true"},
            {"key": "bridge-source-arn", "value": original_arn},
        ],
    )
    new_arn = create_resp["resourceShare"]["resourceShareArn"]

    # Associate remaining resources in batches of 100
    for i in range(100, len(resource_arns), 100):
        batch = resource_arns[i : i + 100]
        print(f"[DEBUG]   Associating resource batch ({len(batch)} resources, offset {i})")
        ram.associate_resource_share(
            resourceShareArn=new_arn,
            resourceArns=batch,
        )

    # Associate remaining principals in batches of 100
    for i in range(100, len(principal_ids), 100):
        batch = principal_ids[i : i + 100]
        print(f"[DEBUG]   Associating principal batch ({len(batch)} principals, offset {i})")
        ram.associate_resource_share(
            resourceShareArn=new_arn,
            principals=batch,
        )

    return new_arn


def wait_for_associations(resource_share_arn: str, max_attempts: int = 12, delay: int = 10):
    for attempt in range(1, max_attempts + 1):
        print(f"[DEBUG]   Checking association status (attempt {attempt}/{max_attempts})")
        details = get_share_details(resource_share_arn)
        non_associated = []
        for kind in ("resources", "principals", "permissions"):
            for item in details[kind]:
                status = item.get("status", "")
                if status != "ASSOCIATED":
                    name = item.get("associatedEntity", "unknown")
                    non_associated.append(f"{kind}:{name}={status}")
        if not non_associated:
            print(f"[DEBUG]   All associations reached ASSOCIATED state")
            return
        print(f"[DEBUG]   {len(non_associated)} not yet ASSOCIATED: {non_associated[:5]}")
        time.sleep(delay)
    raise TimeoutError(
        f"Not all associations reached ASSOCIATED for {resource_share_arn} after {max_attempts} attempts. "
        f"Still pending: {non_associated[:10]}"
    )


def main():
    global ram
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True)
    parser.add_argument("--execute", action="store_true", default=False)
    args = parser.parse_args()
    ram = boto3.client("ram", region_name=args.region)
    account_id = boto3.client("sts").get_caller_identity()["Account"]

    if not args.execute:
        print("[DRYRUN] Running in dry-run mode. No shares will be created or invitations accepted.")

    # Track internal principals for output file
    output_records = []

    # Find all principals where external=False
    print(f"[DEBUG] Listing all principals in region {args.region}...")
    all_principals = paginate("list_principals", "principals", resourceOwner="SELF")
    print(f"[DEBUG] Found {len(all_principals)} total principals")

    internal_principals = [p for p in all_principals if not p.get("external", True)]
    print(f"[DEBUG] Found {len(internal_principals)} internal (non-external) principals")

    for p in internal_principals:
        output_records.append({
            "principalId": p["id"],
            "resourceShareArn": p["resourceShareArn"],
        })

    # Group by resource share ARN
    share_arns = {p["resourceShareArn"] for p in internal_principals}
    print(f"[DEBUG] Found {len(share_arns)} unique resource shares to duplicate")

    new_share_arns = []
    for share_arn in share_arns:
        print(f"\n[DEBUG] Processing share: {share_arn}")
        details = get_share_details(share_arn)
        print(f"[DEBUG]   Resources: {len(details['resources'])}, Principals: {len(details['principals'])}, Permissions: {len(details['permissions'])}")

        has_glue = any(":glue:" in r["associatedEntity"] for r in details["resources"])
        if not has_glue:
            print(f"[DEBUG]   Skipping share {share_arn} — no Glue resources found")
            continue

        if args.execute:
            new_arn = create_duplicate_share(share_arn, details)
            print(f"[DEBUG]   Created duplicate share: {new_arn}")
        else:
            new_arn = share_arn
            print(f"[DRYRUN]  Would create bridge share from {share_arn} with {len(details['resources'])} resources, {len(details['principals'])} principals, {len(details['permissions'])} permissions")

        for principal in details["principals"]:
            print(f"[DEBUG]   Calling accept_invitation for principal {principal['associatedEntity']}")
            accept_invitation(new_arn, principal["associatedEntity"], "OrganizationAccountAccessRole", args.execute)

        new_share_arns.append(new_arn)

    # Validate all associations after all shares are created and invitations accepted
    for new_arn in new_share_arns:
        if args.execute:
            print(f"\n[DEBUG] Validating associations for {new_arn}")
            wait_for_associations(new_arn)
        else:
            print(f"[DRYRUN]  Would validate all associations reached ASSOCIATED state for {new_arn}")

    # Write output file
    output_file = f"internal_principals_{account_id}_{args.region}.json"
    with open(output_file, "w") as f:
        json.dump(output_records, f, indent=2)
    print(f"\n[DEBUG] Wrote {len(output_records)} records to {output_file}")


if __name__ == "__main__":
    main()
