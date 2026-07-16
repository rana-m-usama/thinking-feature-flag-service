# terraform init -backend-config=backend.production.hcl -reconfigure
# Use the Makefile: `make init ENV=production`
bucket = "thinking-flagsvc-0e6b-tfstate"
prefix = "flagsvc/production"
