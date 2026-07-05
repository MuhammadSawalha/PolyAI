FROM python:3.12-slim-trixie

WORKDIR /app

RUN apt-get update && apt-get install -y libgl1-mesa-dev libglib2.0-0 && rm -rf /var/lib/apt/lists/*


COPY torch-requirements.txt requirements.txt ./

RUN pip install --no-cache-dir --upgrade pip wheel setuptools && \
    pip install --no-cache-dir -r torch-requirements.txt && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "app.py"]
