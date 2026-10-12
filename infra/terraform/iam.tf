# ---------------------------------------------------------------------------------------
# Least-privilege roles:
#   task   : what the running API may do
#   exec   : ECS agent (pull image, write logs, read secrets)
#   ingest : one-off ingestion job
#   ci     : GitHub Actions via OIDC (eval + prompt publishing + deploy)
# ---------------------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

locals {
  bedrock_models = [
    "arn:aws:bedrock:*::foundation-model/${var.embed_model_id}",
    "arn:aws:bedrock:*::foundation-model/*", # Converse via inference profile fans out to regional models
    "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
  ]
  rerank_model_arn = "arn:aws:bedrock:${var.rerank_region}::foundation-model/${var.rerank_model_id}"
}

# ---- API task role -------------------------------------------------------------------------
resource "aws_iam_role" "task" {
  name               = "${var.name}-${var.environment}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "BedrockInvoke"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = local.bedrock_models
  }
  statement {
    sid       = "BedrockRerank"
    actions   = ["bedrock:Rerank", "bedrock:InvokeModel"]
    resources = [local.rerank_model_arn]
  }
  statement {
    sid       = "BedrockGuardrail"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = [aws_bedrock_guardrail.this.guardrail_arn]
  }
  statement {
    sid       = "BedrockPromptRead"
    actions   = ["bedrock:GetPrompt"]
    resources = ["arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:prompt/*"]
  }
  statement {
    sid       = "KmsUse"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.this.arn]
  }
}

resource "aws_iam_role_policy" "task" {
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ---- Execution role ------------------------------------------------------------------------
resource "aws_iam_role" "exec" {
  name               = "${var.name}-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "exec_managed" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "exec_secrets" {
  count = var.langfuse_secret_arn == "" ? 0 : 1
  role  = aws_iam_role.exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = var.langfuse_secret_arn },
      { Effect = "Allow", Action = "kms:Decrypt", Resource = aws_kms_key.this.arn }
    ]
  })
}

# ---- Ingestion role (assumed by whoever runs `python -m insurance_rag ingest`) ------------------
resource "aws_iam_role" "ingest" {
  name = "${var.name}-${var.environment}-ingest"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EcsTask"
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
      },
      {
        Sid       = "HumanWithMfa"
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Condition = { Bool = { "aws:MultiFactorAuthPresent" = "true" } }
      }
    ]
  })
}

data "aws_iam_policy_document" "ingest" {
  statement {
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:*::foundation-model/${var.embed_model_id}"]
  }
  statement {
    actions   = ["comprehend:DetectPiiEntities", "comprehendmedical:DetectPHI"]
    resources = ["*"]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.this.arn]
  }
}

resource "aws_iam_role_policy" "ingest" {
  role   = aws_iam_role.ingest.id
  policy = data.aws_iam_policy_document.ingest.json
}

# ---- CI role (GitHub OIDC; no long-lived keys) --------------------------------------------------
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.github_repo == "" ? 0 : 1
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "ci" {
  count = var.github_repo == "" ? 0 : 1
  name  = "${var.name}-${var.environment}-ci"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.github[0].arn }
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*" }
      }
    }]
  })
}

data "aws_iam_policy_document" "ci" {
  statement {
    sid       = "EvalBedrock"
    actions   = ["bedrock:InvokeModel", "bedrock:Rerank", "bedrock:ApplyGuardrail"]
    resources = ["*"]
  }
  statement {
    sid       = "EvalResults"
    actions   = ["dynamodb:PutItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.eval.arn]
  }
  statement {
    sid = "PromptPublish"
    actions = [
      "bedrock:CreatePrompt", "bedrock:UpdatePrompt", "bedrock:CreatePromptVersion",
      "bedrock:ListPrompts", "bedrock:GetPrompt"
    ]
    resources = ["*"]
  }
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage"
    ]
    resources = ["*"]
  }
  statement {
    sid       = "Deploy"
    actions   = ["ecs:UpdateService", "ecs:RegisterTaskDefinition", "ecs:DescribeServices", "ecs:DescribeTaskDefinition", "iam:PassRole"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ci" {
  count  = var.github_repo == "" ? 0 : 1
  role   = aws_iam_role.ci[0].id
  policy = data.aws_iam_policy_document.ci.json
}
