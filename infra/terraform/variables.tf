variable "region" {
  type    = string
  default = "us-east-1"
}

variable "rerank_region" {
  type        = string
  default     = "us-west-2"
  description = "Region whose Bedrock offers the rerank model. Verify in the console."
}

variable "environment" {
  type    = string
  default = "staging"
}

variable "name" {
  type    = string
  default = "insurance-rag"
}

# ---- network (bring your own VPC; see docs/DEPLOYMENT.md for required endpoints) ----
variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets with a route to Bedrock (VPC endpoints or NAT)."
}

# ---- application ----
variable "image_tag" {
  type        = string
  description = "Image tag in the ECR repository to deploy (the git SHA in CI)."
}

variable "llm_model_id" {
  type    = string
  default = "us.amazon.nova-lite-v1:0"
}

variable "embed_model_id" {
  type    = string
  default = "amazon.titan-embed-text-v2:0"
}

variable "rerank_model_id" {
  type    = string
  default = "amazon.rerank-v1:0"
}

variable "prompt_id" {
  type        = string
  default     = ""
  description = "Bedrock Prompt Management id (empty = use the prompt baked into the image)."
}

variable "prompt_version" {
  type    = string
  default = ""
}

variable "corpus_version" {
  type        = string
  default     = "v1"
  description = "Bump after re-ingesting to invalidate the semantic cache."
}

variable "langfuse_host" {
  type    = string
  default = ""
}

variable "langfuse_secret_arn" {
  type        = string
  default     = ""
  description = "Secrets Manager secret holding JSON {public_key, secret_key}. Empty disables Langfuse."
}

# ---- sizing (defaults are small and cheap; scale up for production) ----
variable "task_cpu" {
  type    = number
  default = 1024
}

variable "task_memory" {
  type    = number
  default = 2048
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "opensearch_instance_type" {
  type    = string
  default = "t3.small.search"
}

variable "opensearch_instance_count" {
  type    = number
  default = 1
}

variable "cache_node_type" {
  type    = string
  default = "cache.t4g.small"
}

# ---- governance ----
variable "create_cloudtrail" {
  type        = bool
  default     = false
  description = "Create a dedicated trail. Set false if your organisation already trails this account."
}

variable "github_repo" {
  type        = string
  default     = ""
  description = "owner/repo allowed to assume the CI role via OIDC. Empty skips the CI role."
}

variable "alarm_email" {
  type    = string
  default = ""
}
