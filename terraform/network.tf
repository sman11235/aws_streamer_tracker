data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "app" {
  cidr_block           = local.settings.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
}

resource "aws_internet_gateway" "app" {
  vpc_id = aws_vpc.app.id
}

resource "aws_subnet" "public" {
  for_each = { for index, az in slice(data.aws_availability_zones.available.names, 0, local.infrastructure.network.availability_zone_count) : index => az }

  vpc_id                  = aws_vpc.app.id
  cidr_block              = cidrsubnet(local.settings.vpc_cidr, local.infrastructure.network.subnet_newbits, each.key)
  availability_zone       = each.value
  map_public_ip_on_launch = true
}

resource "aws_subnet" "private" {
  for_each = { for index, az in slice(data.aws_availability_zones.available.names, 0, local.infrastructure.network.availability_zone_count) : index => az }

  vpc_id            = aws_vpc.app.id
  cidr_block        = cidrsubnet(local.settings.vpc_cidr, local.infrastructure.network.subnet_newbits, each.key + local.infrastructure.network.private_subnet_offset)
  availability_zone = each.value
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.app.id

  route {
    cidr_block = local.infrastructure.network.outbound_cidr
    gateway_id = aws_internet_gateway.app.id
  }
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  domain = "vpc"
}

resource "aws_nat_gateway" "app" {
  allocation_id = aws_eip.nat.id
  subnet_id     = values(aws_subnet.public)[0].id

  depends_on = [aws_internet_gateway.app, aws_route_table_association.public]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.app.id

  route {
    cidr_block     = local.infrastructure.network.outbound_cidr
    nat_gateway_id = aws_nat_gateway.app.id
  }
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

resource "aws_security_group" "lambda" {
  name        = "${local.prefix}-lambda"
  description = "Network access for streamer tracker Lambda functions."
  vpc_id      = aws_vpc.app.id

  egress {
    description = "HTTPS access to AWS, YouTube, and Twitch APIs."
    from_port   = local.infrastructure.network.https_port
    to_port     = local.infrastructure.network.https_port
    protocol    = "tcp"
    cidr_blocks = [local.infrastructure.network.outbound_cidr]
  }

  egress {
    description = "PostgreSQL access within the application VPC."
    from_port   = local.runtime_config.database.port
    to_port     = local.runtime_config.database.port
    protocol    = "tcp"
    cidr_blocks = [local.settings.vpc_cidr]
  }
}

resource "aws_security_group" "postgres" {
  name        = "${local.prefix}-postgres"
  description = "PostgreSQL access from streamer tracker Lambda functions."
  vpc_id      = aws_vpc.app.id

  ingress {
    description     = "PostgreSQL from Lambda functions."
    from_port       = local.runtime_config.database.port
    to_port         = local.runtime_config.database.port
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda.id]
  }
}
