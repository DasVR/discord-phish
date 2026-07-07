COMPOSE := docker compose
SERVICE := research-bot

.PHONY: up down restart logs stop start rebuild shell

up: ## Build and start the research bot in the background
	@test -f state.json || echo '{"sessions_enabled": true, "total_completed": 0}' > state.json
	$(COMPOSE) up -d --build

down: ## Stop and remove containers
	$(COMPOSE) down

restart: ## Restart the research bot container
	$(COMPOSE) restart $(SERVICE)

logs: ## Follow container logs
	$(COMPOSE) logs -f $(SERVICE)

stop: ## Stop the research bot without removing the container
	$(COMPOSE) stop $(SERVICE)

start: ## Start a stopped research bot container
	$(COMPOSE) start $(SERVICE)

rebuild: ## Rebuild image from scratch and restart
	$(COMPOSE) build --no-cache $(SERVICE)
	$(COMPOSE) up -d $(SERVICE)

shell: ## Open a shell inside the running container
	$(COMPOSE) exec $(SERVICE) /bin/sh
