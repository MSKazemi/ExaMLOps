FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# Install only the slim serving dependencies — no torch/CUDA needed for sklearn inference
COPY services/ray_serving/requirements.txt .
RUN uv pip install --no-cache-dir -r requirements.txt --system

COPY services/ray_serving/app.py .

EXPOSE 8001 8265

CMD ["python", "app.py"]
