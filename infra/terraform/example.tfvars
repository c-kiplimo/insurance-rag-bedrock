# cp example.tfvars staging.tfvars  (staging.tfvars is git-ignored), then:
#   terraform init
#   terraform plan  -var-file=staging.tfvars -var image_tag=bootstrap
#   terraform apply -var-file=staging.tfvars -var image_tag=bootstrap

region             = "us-east-1"
rerank_region      = "us-west-2"
environment        = "staging"
vpc_id             = "vpc-0123456789abcdef0"
private_subnet_ids = ["subnet-aaaa", "subnet-bbbb"]

github_repo       = "your-org/insurance-rag"
create_cloudtrail = false
alarm_email       = "you@example.com"

# Leave empty on the first apply; CI fills these in after publishing a prompt version.
prompt_id      = ""
prompt_version = ""
