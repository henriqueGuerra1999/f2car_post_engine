FROM python:3.11-slim

# Fontes Poppins (usadas pelo motor de renderizacao) -- Debian tem-nas no
# pacote fonts-open-sans/fonts-google-*; instalamos a familia completa via
# fonts-poppins se existir, senao caimos para a DejaVu que o Pillow ja traz.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# NOTA: se as fontes Poppins-*.ttf nao existirem no host de deploy (o
# generate_post.py aponta para /usr/share/fonts/truetype/google-fonts/),
# copiar os .ttf para dentro da imagem em ./fonts/ e ajustar FONT_DIR em
# generate_post.py antes do deploy final.

EXPOSE 8000
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000"]
