# encryption.rego — OPA policy for S3 bucket encryption
# Maps to: PCIDSS-REQ3.4 (Render PAN Unreadable)
#
# Denies S3 bucket resources that lack server-side encryption configuration.
# Used by conftest in the compliance-gate GitHub Actions workflow.

package compliance.encryption

import rego.v1

# Deny S3 buckets without server-side encryption
deny contains msg if {
	some resource in input.resource_changes
	resource.type == "aws_s3_bucket"
	not has_encryption(resource)
	msg := sprintf(
		"PCIDSS-REQ3.4 violation: S3 bucket '%s' does not have server_side_encryption_configuration. All S3 buckets must encrypt data at rest.",
		[resource.name],
	)
}

# Deny S3 buckets that reference encryption but use a weak algorithm
deny contains msg if {
	some resource in input.resource_changes
	resource.type == "aws_s3_bucket"
	has_encryption(resource)
	enc := resource.change.after.server_side_encryption_configuration[0]
	rule := enc.rule[0]
	algorithm := rule.apply_server_side_encryption_by_default[0].sse_algorithm
	algorithm != "aws:kms"
	algorithm != "AES256"
	msg := sprintf(
		"PCIDSS-REQ3.4 violation: S3 bucket '%s' uses unsupported encryption algorithm '%s'. Use AES256 or aws:kms.",
		[resource.name, algorithm],
	)
}

has_encryption(resource) if {
	resource.change.after.server_side_encryption_configuration
	count(resource.change.after.server_side_encryption_configuration) > 0
}
