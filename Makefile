.PHONY: install test eval web deploy-agent-engine deploy-cloud-run terraform-plan

install:
	pip install -e ".[dev]"

test:
	python3 -m unittest discover -s tests -v

eval:
	adk eval flight_scanner_agent evals/flight_scanner.evalset.json --config_file_path=evals/test_config.json

web:
	adk web --session_service_uri="sqlite:///./data/flight_sessions.db" --trace_to_cloud

deploy-agent-engine:
	adk deploy agent_engine \
		--project="$(GOOGLE_CLOUD_PROJECT)" \
		--region="us-central1" \
		--staging_bucket="gs://$(GOOGLE_CLOUD_PROJECT)-agent-staging" \
		--display_name="flight-deal-scanner-agent" \
		--trace_to_cloud \
		flight_scanner_agent

deploy-cloud-run:
	gcloud run deploy flight-deal-scanner-agent \
		--source . \
		--region us-central1 \
		--set-secrets="SERPAPI_API_KEY=SERPAPI_API_KEY:latest,TRIP_COM_PARTNER_KEY=TRIP_COM_PARTNER_KEY:latest" \
		--no-allow-unauthenticated

terraform-plan:
	cd terraform && terraform init -backend=false && terraform validate
