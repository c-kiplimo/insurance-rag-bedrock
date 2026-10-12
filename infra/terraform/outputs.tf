output "api_url" {
  value       = aws_apigatewayv2_api.this.api_endpoint
  description = "POST {api_url}/v1/query (SigV4-signed, AWS_IAM auth)"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "opensearch_endpoint" {
  value = "https://${aws_opensearch_domain.this.endpoint}"
}

output "eval_table" {
  value = aws_dynamodb_table.eval.name
}

output "guardrail_id" {
  value = aws_bedrock_guardrail.this.guardrail_id
}

output "ingest_role_arn" {
  value = aws_iam_role.ingest.arn
}

output "ci_role_arn" {
  value = try(aws_iam_role.ci[0].arn, null)
}

output "ingest_task_definition" {
  value = aws_ecs_task_definition.ingest.family
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}
