# ---------------------------------------------------------------------------------------
# Encryption key, security groups, and the Bedrock guardrail.
# ---------------------------------------------------------------------------------------

resource "aws_kms_key" "this" {
  description             = "${var.name} data encryption (OpenSearch, cache, DynamoDB, logs, trail)"
  enable_key_rotation     = true
  deletion_window_in_days = 14

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdmin"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "CloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
      },
      {
        Sid       = "CloudTrail"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = ["kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource  = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.this.key_id
}

# Traffic path: API Gateway -> VPC link -> ALB -> ECS -> {OpenSearch, cache, Bedrock endpoints}
resource "aws_security_group" "alb" {
  name_prefix = "${var.name}-alb-"
  vpc_id      = var.vpc_id
  description = "Internal ALB, reachable only from the API Gateway VPC link"
  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "vpc_link" {
  name_prefix = "${var.name}-vpclink-"
  vpc_id      = var.vpc_id
  description = "API Gateway VPC link ENIs"
  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "app" {
  name_prefix = "${var.name}-app-"
  vpc_id      = var.vpc_id
  description = "ECS tasks"
  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "data" {
  name_prefix = "${var.name}-data-"
  vpc_id      = var.vpc_id
  description = "OpenSearch and cache: reachable only from the app"
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_egress_rule" "vpc_link_to_alb" {
  security_group_id            = aws_security_group.vpc_link.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                    = 80
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_vpc_link" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.vpc_link.id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_app" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_ingress_rule" "app_from_alb" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

# The app needs outbound HTTPS (Bedrock/ECR/Secrets via endpoints or NAT) plus data stores.
resource "aws_vpc_security_group_egress_rule" "app_https" {
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = data.aws_vpc.this.cidr_block
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "VPC endpoints, OpenSearch"
}

resource "aws_vpc_security_group_egress_rule" "app_internet_https" {
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Bedrock rerank region / Langfuse via NAT. Remove if everything is via endpoints."
}

resource "aws_vpc_security_group_egress_rule" "app_to_cache" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.data.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

resource "aws_vpc_security_group_ingress_rule" "data_opensearch_from_app" {
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "data_cache_from_app" {
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

# ---- Bedrock guardrail -----------------------------------------------------------------
resource "aws_bedrock_guardrail" "this" {
  name                      = "${var.name}-${var.environment}"
  description               = "Prompt-attack, PII and contextual-grounding checks for insurance Q&A"
  blocked_input_messaging   = "This request cannot be processed because it conflicts with usage policy."
  blocked_outputs_messaging = "This response was withheld because it did not meet quality or safety checks."
  kms_key_arn               = aws_kms_key.this.arn

  content_policy_config {
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE" # the API requires NONE for prompt-attack on output
    }
    filters_config {
      type            = "HATE"
      input_strength  = "HIGH"
      output_strength = "HIGH"
    }
    filters_config {
      type            = "INSULTS"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }
    filters_config {
      type            = "VIOLENCE"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }
  }

  # Mask identifiers in user input before they reach the model, cache or traces.
  sensitive_information_policy_config {
    dynamic "pii_entities_config" {
      for_each = ["US_SOCIAL_SECURITY_NUMBER", "PHONE", "EMAIL", "CREDIT_DEBIT_CARD_NUMBER", "US_BANK_ACCOUNT_NUMBER", "DRIVER_ID"]
      content {
        type          = pii_entities_config.value
        action        = "ANONYMIZE"
        input_action  = "ANONYMIZE"
        output_action = "ANONYMIZE"
      }
    }
  }

  # Low-hallucination control: the answer must be grounded in the retrieved sources.
  contextual_grounding_policy_config {
    filters_config {
      type      = "GROUNDING"
      threshold = 0.75
    }
    filters_config {
      type      = "RELEVANCE"
      threshold = 0.5
    }
  }
}

resource "aws_bedrock_guardrail_version" "this" {
  guardrail_arn = aws_bedrock_guardrail.this.guardrail_arn
  description   = "managed by terraform"
}
