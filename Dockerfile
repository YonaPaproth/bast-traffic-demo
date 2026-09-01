FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install DuckDB httpfs extension so the container doesn't need internet at runtime
RUN python -c "import duckdb; con = duckdb.connect(); con.execute('INSTALL httpfs'); con.close()"

COPY api/ ./api/

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
