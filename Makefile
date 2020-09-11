# lambda function attributions
FUNCTION_NAME := lambda-spot-interruption
DESCRIPTION := "Lambda function used to update ASG and drain ELB in case of spot termination"
REGION := us-east-1
ZIP_FILE := lambda-spot-interruption.zip
LAMBDA_ROLE :=
HANDLER := main.handler
RUNTIME := python3.7
TIMEOUT := 30
MEMORY_SIZE := 192
TAGS :=
ROLE_NAME :=
ENV_VARS := "{ROLE_NAME=$(ROLE_NAME)}"
CUSTOM_ARGS :=
VPC_SUBNETS := "subnet-42d39634"
VPC_SGS := "sg-0f11a9dd10098e54f"

PKG_FILE := "$(shell pwd)/$(ZIP_FILE)"
REQUIRED_BINS := python3.7 pip3.7

ifneq ($(TAGS),)
CUSTOM_ARGS := $(CUSTOM_ARGS) --tags "$(TAGS)"
endif

.PHONY: dependencies
dependencies:
	$(foreach bin,$(REQUIRED_BINS),\
		$(if $(shell command -v $(bin) 2>/dev/null),$(info Found `$(bin)`),\
			$(error "Could not find `$(bin)` in PATH=$(PATH), consider installing from package manager or from source")))
	( \
		if [ ! -d "./venv" ]; then \
			virtualenv -p python3.7 venv; \
		fi; \
		. venv/bin/activate; \
		pip3.7 install -r requirements.txt; \
		deactivate; \
	)

.PHONY : pack
pack:
	( \
		rm -rf $(PKG_FILE); \
		mkdir -p package; \
		cat requirements.txt | grep -v boto | xargs pip$(RUNTIME_VERSION) install --upgrade --target ./package ; \
		cd package/; \
		zip -r9 ../$(ZIP_FILE) .; \
		cd -; \
		zip -g $(ZIP_FILE) main.py; \
	)

.PHONY : create-function
create-function:
	test -n "$(LAMBDA_ROLE)" # Empty LAMBDA_ROLE variable
	test -n "$(ROLE_NAME)" # Empty ROLE_NAME variable
	aws lambda create-function \
	--function-name $(FUNCTION_NAME) \
	--description $(DESCRIPTION) \
	--region $(REGION) \
	--zip-file fileb://$(ZIP_FILE) \
	--role $(LAMBDA_ROLE) \
	--handler $(HANDLER) \
	--runtime $(RUNTIME) \
	--timeout $(TIMEOUT) \
	--memory-size $(MEMORY_SIZE) \
	--vpc-config SubnetIds=$(VPC_SUBNETS),SecurityGroupIds=$(VPC_SGS) \
	--environment Variables=$(ENV_VARS) $(CUSTOM_ARGS)
	#--environment Variables=$(ENV_VARS) $(CUSTOM_ARGS)

.PHONY : update-function
update-function:
	aws lambda update-function-code --function-name $(FUNCTION_NAME) --zip-file fileb://$(ZIP_FILE) --region $(REGION)

.PHONY: deploy
deploy: dependencies pack update-function
