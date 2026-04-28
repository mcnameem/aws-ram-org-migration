#!/usr/bin/env python

import argparse
import json
import time
import boto3
from collections import defaultdict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--region", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--execute", action="store_true", help="Perform changes against AWS.")
    mode.add_argument("--dry-run", action="store_true", help="Preview changes without calling mutating APIs.")
    args = parser.parse_args()

    ram = boto3.client("ram", region_name=args.region)
    caller_account = boto3.client("sts").get_caller_identity()["Account"]

    with open(args.input_file) as f:
        records = json.load(f)

    # Verify credentials match the account in the share ARNs
    share_accounts = {r["resourceShareArn"].split(":")[4] for r in records}
    if share_accounts != {caller_account}:
        print(f"[ERROR] Credential account {caller_account} does not match share ARN account(s): {share_accounts}")
        return

    print(f"[DEBUG] Credential account {caller_account} matches share ARN account(s)")

    if not args.execute:
        print("[DRYRUN] Running in dry-run mode. No associations will be made.")

    # Group principals by share ARN
    share_to_principals = defaultdict(list)
    for r in records:
        share_to_principals[r["resourceShareArn"]].append(r["principalId"])

    for share_arn, principal_ids in share_to_principals.items():
        print(f"[DEBUG] Associating {len(principal_ids)} principals back into {share_arn}")

        if not args.execute:
            for pid in principal_ids:
                print(f"[DRYRUN]  Would associate principal {pid} into {share_arn}")
            continue

        # Associate in batches of 100
        for i in range(0, len(principal_ids), 100):
            batch = principal_ids[i : i + 100]
            print(f"[DEBUG]   Associating batch of {len(batch)} principals (offset {i})")
            try:
                ram.associate_resource_share(
                    resourceShareArn=share_arn,
                    principals=batch,
                )
            except ram.exceptions.OperationNotPermittedException:
                print(f"[WARN]   OperationNotPermittedException — setting allowExternalPrincipals=true on {share_arn}")
                ram.update_resource_share(
                    resourceShareArn=share_arn,
                    allowExternalPrincipals=True,
                )
                time.sleep(3)
                ram.associate_resource_share(
                    resourceShareArn=share_arn,
                    principals=batch,
                )

        # Confirm each principal reaches ASSOCIATED state
        for pid in principal_ids:
            max_attempts = 10
            associated = False
            for attempt in range(1, max_attempts + 1):
                print(f"[DEBUG]   Checking association status for {pid} (attempt {attempt}/{max_attempts})")
                resp = ram.get_resource_share_associations(
                    associationType="PRINCIPAL",
                    resourceShareArns=[share_arn],
                    principal=pid,
                )
                associations = resp.get("resourceShareAssociations", [])
                if any(a["status"] == "ASSOCIATED" for a in associations):
                    print(f"[DEBUG]   Principal {pid} is ASSOCIATED in {share_arn}")
                    associated = True
                    break
                time.sleep(5)

            if not associated:
                # Still ASSOCIATING — try accepting the invitation from the principal's account
                print(f"[WARN]   Principal {pid} still not ASSOCIATED, attempting to accept invitation from account {pid}")
                try:
                    sts = boto3.client("sts")
                    creds = sts.assume_role(
                        RoleArn=f"arn:aws:iam::{pid}:role/OrganizationAccountAccessRole",
                        RoleSessionName="accept-ram-invitation",
                    )["Credentials"]
                    remote_ram = boto3.client(
                        "ram",
                        region_name=args.region,
                        aws_access_key_id=creds["AccessKeyId"],
                        aws_secret_access_key=creds["SecretAccessKey"],
                        aws_session_token=creds["SessionToken"],
                    )
                    invitations = remote_ram.get_resource_share_invitations()["resourceShareInvitations"]
                    match = [i for i in invitations if i["resourceShareArn"] == share_arn and i["status"] == "PENDING"]
                    if match:
                        invitation_arn = match[0]["resourceShareInvitationArn"]
                        print(f"[DEBUG]   Found invitation {invitation_arn}, accepting...")
                        remote_ram.accept_resource_share_invitation(resourceShareInvitationArn=invitation_arn)
                        print(f"[DEBUG]   Invitation accepted in account {pid}")
                    else:
                        print(f"[WARN]   No pending invitation found for {share_arn} in account {pid}")
                except Exception as e:
                    print(f"[ERROR]   Failed to accept invitation in account {pid}: {e}")

                # Re-check for ASSOCIATED in the share owner account
                for attempt in range(1, max_attempts + 1):
                    print(f"[DEBUG]   Re-checking association status for {pid} (attempt {attempt}/{max_attempts})")
                    resp = ram.get_resource_share_associations(
                        associationType="PRINCIPAL",
                        resourceShareArns=[share_arn],
                        principal=pid,
                    )
                    associations = resp.get("resourceShareAssociations", [])
                    if any(a["status"] == "ASSOCIATED" for a in associations):
                        print(f"[DEBUG]   Principal {pid} is ASSOCIATED in {share_arn}")
                        associated = True
                        break
                    time.sleep(5)

                if not associated:
                    print(f"[ERROR]   Principal {pid} did not reach ASSOCIATED state in {share_arn} after accept attempt")

    print("\n[DEBUG] Done.")


if __name__ == "__main__":
    main()
