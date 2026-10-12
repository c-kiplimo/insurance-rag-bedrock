# ---------------------------------------------------------------------------------------
# Data stores: OpenSearch (hybrid index), ElastiCache for Valkey (semantic cache),
# DynamoDB (evaluation results).
# ---------------------------------------------------------------------------------------

# ---- OpenSearch managed domain ----------------------------------------------------------
# A managed domain (not Serverless) because hybrid search needs search pipelines.
# Access is IAM-only (SigV4) inside the VPC; there is no public endpoint.
resource "aws_opensearch_domain" "this" {
  domain_name    = "${var.name}-${var.environment}"
  engine_version = "OpenSearch_2.17"

  cluster_config {
    instance_type  = var.opensearch_instance_type
    instance_count = var.opensearch_instance_count
  }

  ebs_options {
    ebs_enabled = true
    volume_type = "gp3"
    volume_size = 20
  }

  vpc_options {
    # one subnet per node group; with 1 node a single subnet is required
    subnet_ids         = slice(var.private_subnet_ids, 0, min(var.opensearch_instance_count, length(var.private_subnet_ids)))
    security_group_ids = [aws_security_group.data.id]
  }

  encrypt_at_rest {
    enabled    = true
    kms_key_id = aws_kms_key.this.key_id
  }

  node_to_node_encryption {
    enabled = true
  }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = [aws_iam_role.task.arn, aws_iam_role.ingest.arn] }
      Action    = "es:ESHttp*"
      Resource  = "arn:aws:es:${var.region}:${data.aws_caller_identity.current.account_id}:domain/${var.name}-${var.environment}/*"
    }]
  })

  log_publishing_options {
    log_type                 = "ES_APPLICATION_LOGS"
    cloudwatch_log_group_arn = aws_cloudwatch_log_group.opensearch.arn
  }

  depends_on = [aws_cloudwatch_log_resource_policy.opensearch]
}

resource "aws_cloudwatch_log_group" "opensearch" {
  name              = "/aws/opensearch/${var.name}-${var.environment}"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.this.arn
}

resource "aws_cloudwatch_log_resource_policy" "opensearch" {
  policy_name = "${var.name}-opensearch-logs"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "es.amazonaws.com" }
      Action    = ["logs:PutLogEvents", "logs:CreateLogStream"]
      Resource  = "${aws_cloudwatch_log_group.opensearch.arn}:*"
    }]
  })
}

# ---- Semantic cache ----------------------------------------------------------------------
# Needs vector search: ElastiCache for Valkey 8.2+ (verify availability in your region), or
# swap for MemoryDB. If unavailable set RAG_CACHE_MODE=exact (hash-only, works on any engine).
resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name}-${var.environment}"
  subnet_ids = var.private_subnet_ids
}

resource "aws_elasticache_replication_group" "cache" {
  replication_group_id       = "${var.name}-${var.environment}"
  description                = "Semantic cache for ${var.name}"
  engine                     = "valkey"
  engine_version             = "8.2"
  node_type                  = var.cache_node_type
  num_cache_clusters         = var.environment == "prod" ? 2 : 1
  automatic_failover_enabled = var.environment == "prod"
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [aws_security_group.data.id]
  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.this.arn
  transit_encryption_enabled = true
  snapshot_retention_limit   = 0 # a cache: rebuilds itself
}

# ---- Evaluation results --------------------------------------------------------------------
# On-demand billing: scales to zero between pipeline runs.
resource "aws_dynamodb_table" "eval" {
  name         = "${var.name}-eval-results"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "commit_sha"
  range_key    = "run_id"

  attribute {
    name = "commit_sha"
    type = "S"
  }
  attribute {
    name = "run_id"
    type = "S"
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.this.arn
  }
}
