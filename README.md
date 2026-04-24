# RAM Bridge Share Scripts

Three scripts for managing AWS RAM (Resource Access Manager) bridge shares. Bridge shares are duplicates of organization-internal shares that enable cross-account resource sharing to survive an account leaving an organization.

All scripts support a `--execute` flag. Without it they run in dry-run mode and make no changes.

---

## Prerequisites

- Python 3.10+
- `boto3`

### Credentials

These scripts operate against a single account at a time. You must have credentials configured for the account that owns the RAM shares.

**Source account** — The credentials used to run the scripts need RAM permissions in the source account (e.g., `ram:*`).

**Cross-account role** — `create_bridge_shares.py` assumes `OrganizationAccountAccessRole` in each principal's account to accept RAM invitations. That role in the target accounts needs the following permissions:

- `ram:GetResourceShareInvitations`
- `ram:AcceptResourceShareInvitation`

---

## Typical Workflow

1. **`create_bridge_shares.py`** — Create bridge shares and accept invitations. Once complete, it is safe to perform the organization migration.
2. *(Perform the org migration)*
3. **`restore_principals.py`** — After the migration is completed, re-associate internal principals back into their original shares. Do not run this until the migration has finished.
4. **`delete_bridge_shares.py`** — Run last, after migration is completed and principals are restored. This is a cleanup step to remove the bridge shares when the customer is ready.

---

## create_bridge_shares.py

Creates bridge shares from existing RAM shares that contain internal (non-external) principals and Glue resources.

### What it does

1. Lists all principals owned by the caller and filters to internal (non-external) principals
2. Groups those principals by resource share ARN
3. For each share, fetches its resources, principals, and permissions
4. Skips shares that don't contain any AWS Glue resources
5. Creates a duplicate "bridge" share with:
   - `allowExternalPrincipals=True`
   - `retainSharingOnAccountLeaveOrganization=True`
   - Tags: `bridge-share=true` and `bridge-source-arn=<original ARN>`
6. Assumes `OrganizationAccountAccessRole` in each principal's account to accept the RAM invitation
7. Writes a JSON file (`internal_principals_<account>_<region>.json`) recording all internal principal/share mappings

### Usage

```bash
# Dry run
python create_bridge_shares.py --region us-east-1

# Execute
python create_bridge_shares.py --region us-east-1 --execute
```

### Output

`internal_principals_<account_id>_<region>.json` — JSON array of objects with `principalId` and `resourceShareArn`. Used as input to `restore_principals.py`.

---

## restore_principals.py

Re-associates internal principals back into their original RAM shares using the JSON file produced by `create_bridge_shares.py`.

### What it does

1. Reads the input JSON file containing principal-to-share mappings
2. Verifies the caller's account matches the account in the share ARNs
3. Groups principals by share ARN and associates them in batches of 100
4. Polls each principal until it reaches `ASSOCIATED` state (up to 10 attempts, 5s apart)

### Usage

```bash
# Dry run
python restore_principals.py --input-file internal_principals_123456789012_us-east-1.json --region us-east-1

# Execute
python restore_principals.py --input-file internal_principals_123456789012_us-east-1.json --region us-east-1 --execute
```

---

## delete_bridge_shares.py

Deletes bridge shares after confirming the original share still covers all the same resources, principals, and permissions.

### What it does

1. Lists all RAM shares and identifies those tagged with `bridge-share=true`
2. For each bridge share, fetches its associated resources, principals, and permissions
3. Searches for a matching non-bridge share that is a superset of the bridge share's associations
4. Verifies all associations in the matching share are in `ASSOCIATED` state
5. Deletes the bridge share only if both checks pass

### Usage

```bash
# Dry run
python delete_bridge_shares.py --region us-east-1

# Execute
python delete_bridge_shares.py --region us-east-1 --execute
```
