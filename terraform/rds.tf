resource "aws_db_subnet_group" "postgres" {
  name       = "${local.prefix}-postgres"
  subnet_ids = values(aws_subnet.private)[*].id

  description = "Private subnets for the streamer tracker PostgreSQL database."
}

resource "aws_db_instance" "postgres" {
  identifier = "${local.prefix}-postgres"

  engine         = "postgres"
  engine_version = var.postgres_engine_version
  instance_class = var.postgres_instance_class

  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.postgres_database_name
  username = var.postgres_master_username

  # RDS generates and stores the master password in Secrets Manager.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.postgres.name
  vpc_security_group_ids = [aws_security_group.postgres.id]
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period = var.postgres_backup_retention_days
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:00-sun:05:00"

  deletion_protection = false
  skip_final_snapshot = true
  apply_immediately   = true
}
