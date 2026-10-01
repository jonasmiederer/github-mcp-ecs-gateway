# GitHub MCP server image — gateway-brokered 3LO / passthrough.
# Runs privately on ECS Fargate, same as the Method 2 image but with no
# AgentCore Identity code or SDK. See ../docs/CONSENT-UX-options.md.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server.py .

# Streamable-http MCP transport; only the Gateway reaches it (private subnets +
# internal ALB). Never published to the internet.
EXPOSE 8080

# ALB target group health check hits /healthz (see server.py).
CMD ["python", "server.py"]
