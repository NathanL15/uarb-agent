FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
COPY pyproject.toml ./
COPY uarb_agent ./uarb_agent
RUN pip install --no-cache-dir .

ENV UARB_DATA_DIR=/data
VOLUME ["/data"]

CMD ["uarb-agent", "run"]
