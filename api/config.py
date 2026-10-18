from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    weaviate_host: str = "localhost"
    weaviate_port: int = 8080
    ollama_host: str = "localhost"
    ollama_port: int = 11434
    llm_model: str = "phi3.5"
    embed_model: str = "nomic-embed-text"
    upload_dir: str = "/app/uploads"
    # Retained original documents, content-addressed per collection. Kept in
    # its own volume because it grows with the corpus, unlike upload_dir which
    # holds only small config and session files.
    sources_dir: str = "/app/sources"
    # Written export packages. A host bind mount, not a named volume: the
    # whole point is that the user can pick the file up and carry it away.
    exports_dir: str = "/app/exports"
    # Ollama's model store, mounted from the same volume the ollama service
    # uses. Only touched when a package bundles models.
    ollama_models_dir: str = "/ollama"
    # Reported by /health and compared against the memory Docker actually
    # provides. Raise it in docker-compose.yml; no rebuild required.
    recommended_memory_gb: float = 12.0
    # The most an upload's ZIPs may expand to, in bytes, summed over every ZIP
    # in one request, gzipped members' output included (#298). A small ZIP of
    # zeros can otherwise unpack to gigabytes and fill Docker's disk.
    max_zip_expanded_bytes: int = 2 * 1024 * 1024 * 1024

    class Config:
        env_file = ".env"


settings = Settings()
