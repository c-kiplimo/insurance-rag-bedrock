terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80"
    }
  }

  # Remote state: create the bucket + lock table once, then uncomment.
  # backend "s3" {
  #   bucket         = "YOUR-tfstate-bucket"
  #   key            = "insurance-rag/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "YOUR-tfstate-lock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "insurance-rag"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_vpc" "this" {
  id = var.vpc_id
}
