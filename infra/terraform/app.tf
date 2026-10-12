# ---------------------------------------------------------------------------------------
# Application: ECR, ECS Fargate service, internal ALB, API Gateway (HTTP API) via VPC link.
# ---------------------------------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE" # a tag always means the same bits
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.this.arn
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep last 30 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 30 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.name}-${var.environment}"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.this.arn
}

resource "aws_ecs_cluster" "this" {
  name = "${var.name}-${var.environment}"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

locals {
  env = [
    { name = "RAG_ENV", value = var.environment },
    { name = "RAG_AWS_REGION", value = var.region },
    { name = "RAG_LLM_MODEL_ID", value = var.llm_model_id },
    { name = "RAG_EMBED_MODEL_ID", value = var.embed_model_id },
    { name = "RAG_RERANK_REGION", value = var.rerank_region },
    { name = "RAG_RERANK_MODEL_ID", value = var.rerank_model_id },
    { name = "RAG_OPENSEARCH_ENDPOINT", value = "https://${aws_opensearch_domain.this.endpoint}" },
    { name = "RAG_OPENSEARCH_AUTH", value = "sigv4" },
    { name = "RAG_OPENSEARCH_SERVICE", value = "es" },
    { name = "RAG_REDIS_URL", value = "rediss://${aws_elasticache_replication_group.cache.primary_endpoint_address}:6379" },
    { name = "RAG_GUARDRAIL_ID", value = aws_bedrock_guardrail.this.guardrail_id },
    { name = "RAG_GUARDRAIL_VERSION", value = aws_bedrock_guardrail_version.this.version },
    { name = "RAG_GUARDRAIL_GROUNDING", value = "true" },
    { name = "RAG_CORPUS_VERSION", value = var.corpus_version },
    { name = "RAG_PROMPT_ID", value = var.prompt_id },
    { name = "RAG_PROMPT_VERSION", value = var.prompt_version },
    { name = "RAG_LANGFUSE_ENABLED", value = var.langfuse_secret_arn == "" ? "false" : "true" },
    { name = "LANGFUSE_HOST", value = var.langfuse_host },
  ]
  secrets = var.langfuse_secret_arn == "" ? [] : [
    { name = "LANGFUSE_PUBLIC_KEY", valueFrom = "${var.langfuse_secret_arn}:public_key::" },
    { name = "LANGFUSE_SECRET_KEY", valueFrom = "${var.langfuse_secret_arn}:secret_key::" },
  ]
}

resource "aws_ecs_task_definition" "app" {
  family                   = "${var.name}-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  task_role_arn            = aws_iam_role.task.arn
  execution_role_arn       = aws_iam_role.exec.arn

  container_definitions = jsonencode([{
    name         = "api"
    image        = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    environment  = local.env
    secrets      = local.secrets
    readonlyRootFilesystem = true
    linuxParameters        = { initProcessEnabled = true }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

# One-off ingestion job: init index -> download public docs -> ingest. Run it from inside
# the VPC (OpenSearch has no public endpoint):  see docs/DEPLOYMENT.md  "Feed the system".
resource "aws_ecs_task_definition" "ingest" {
  family                   = "${var.name}-${var.environment}-ingest"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 2048
  memory                   = 4096
  task_role_arn            = aws_iam_role.ingest.arn
  execution_role_arn       = aws_iam_role.exec.arn

  container_definitions = jsonencode([{
    name      = "ingest"
    image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}-ingest"
    essential = true
    command = [
      "sh", "-c",
      "python -m insurance_rag init-index && python -m insurance_rag download && python -m insurance_rag generate-claims --count 25 && python -m insurance_rag ingest --redactor regex,comprehend"
    ]
    environment = [for e in local.env : e if contains(["RAG_ENV", "RAG_AWS_REGION", "RAG_EMBED_MODEL_ID", "RAG_OPENSEARCH_ENDPOINT", "RAG_OPENSEARCH_AUTH", "RAG_OPENSEARCH_SERVICE", "RAG_CORPUS_VERSION"], e.name)]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "ingest"
      }
    }
  }])
}

resource "aws_lb" "internal" {
  name_prefix        = "rag-"
  internal           = true
  load_balancer_type = "application"
  subnets            = var.private_subnet_ids
  security_groups    = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "app" {
  name_prefix = "rag-"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  deregistration_delay = 30
  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
  lifecycle { create_before_destroy = true }
}

# Plain HTTP is acceptable ONLY because this ALB is internal and reachable solely from the
# API Gateway VPC link. Add an ACM certificate and HTTPS listener for end-to-end TLS.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.internal.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_ecs_service" "app" {
  name            = "api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "api"
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.http]
}

resource "aws_appautoscaling_target" "app" {
  max_capacity       = 6
  min_capacity       = var.desired_count
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.name}-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  service_namespace  = aws_appautoscaling_target.app.service_namespace
  target_tracking_scaling_policy_configuration {
    target_value = 60
    predefined_metric_specification { predefined_metric_type = "ECSServiceAverageCPUUtilization" }
  }
}

# ---- API Gateway (HTTP API) -> VPC link -> internal ALB -----------------------------------------
resource "aws_apigatewayv2_vpc_link" "this" {
  name               = "${var.name}-${var.environment}"
  subnet_ids         = var.private_subnet_ids
  security_group_ids = [aws_security_group.vpc_link.id]
}

resource "aws_apigatewayv2_api" "this" {
  name          = "${var.name}-${var.environment}"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "alb" {
  api_id             = aws_apigatewayv2_api.this.id
  integration_type   = "HTTP_PROXY"
  integration_method = "ANY"
  integration_uri    = aws_lb_listener.http.arn
  connection_type    = "VPC_LINK"
  connection_id      = aws_apigatewayv2_vpc_link.this.id
}

# AUTH: this route uses AWS_IAM so callers must SigV4-sign. Swap for a JWT authoriser
# (authorization_type = "JWT" + aws_apigatewayv2_authorizer) to integrate your IdP.
resource "aws_apigatewayv2_route" "query" {
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = "POST /v1/query"
  target             = "integrations/${aws_apigatewayv2_integration.alb.id}"
  authorization_type = "AWS_IAM"
}

resource "aws_cloudwatch_log_group" "apigw" {
  name              = "/apigw/${var.name}-${var.environment}"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.this.arn
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 50
    throttling_rate_limit  = 25
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw.arn
    format = jsonencode({
      requestId = "$context.requestId", ip = "$context.identity.sourceIp",
      caller = "$context.identity.caller", route = "$context.routeKey",
      status = "$context.status", latency = "$context.responseLatency",
      integrationStatus = "$context.integrationStatus"
    })
  }
}
