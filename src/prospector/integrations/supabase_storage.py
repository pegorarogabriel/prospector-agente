"""Cópia das imagens dos posts no Supabase Storage: implementação de `ImageStore`.

Ver `docs/arquitetura.md` seção 5. O bucket `post-images` é **privado**: as
imagens têm nome e telefone de gente real. Para o juiz ler uma cópia, o
Storage gera um link assinado que vale por pouco tempo.

A chave usada é a secreta (`sb_secret_...` ou a antiga `service_role`). A
publicável (`sb_publishable_...`) não serve: com o bucket privado e sem
policies, ela não lê nem grava nada, e é assim que deve ser.

A chave vai no cabeçalho, nunca na URL, então um erro de rede não a expõe.
"""

import re
from typing import Any

import httpx

from prospector.core.post import Post

DEFAULT_BUCKET = "post-images"
TIMEOUT_SECONDS = 20.0

#: Imagem maior que isso não é um flyer: é foto em alta, e não vale o espaço.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

#: Quanto tempo o link assinado vale. O backtest usa o link na hora.
LINK_SECONDS = 3600

_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def base_url(raw: str) -> str:
    """A URL do projeto, sem barra no fim nem o `/rest/v1` que o painel às vezes mostra."""
    return raw.strip().rstrip("/").removesuffix("/rest/v1").rstrip("/")


def image_path(post: Post, content_type: str) -> str:
    """`AAAA/MM/<id>.<ext>`: separado por mês, para dar para apagar o que é velho."""
    extension = _EXTENSIONS.get(content_type.split(";")[0].strip().lower(), "jpg")
    safe_id = _UNSAFE.sub("_", post.id)[:120]
    return f"{post.posted_at:%Y/%m}/{safe_id}.{extension}"


class SupabaseImageStore:
    """Baixa a imagem do Facebook e guarda no bucket privado."""

    def __init__(
        self,
        url: str,
        key: str,
        *,
        bucket: str = DEFAULT_BUCKET,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = base_url(url)
        self._bucket = bucket
        self._headers = {"Authorization": f"Bearer {key}", "apikey": key}
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True)
        self.errors: list[str] = []
        """Por que cada cópia falhou, para o relatório. Sem dado de post."""

    def _download(self, url: str) -> tuple[bytes, str] | None:
        response = self._client.get(url)
        content_type = response.headers.get("content-type", "")
        if response.status_code != 200:
            self.errors.append(f"download: HTTP {response.status_code}")
            return None
        if not content_type.startswith("image/"):
            self.errors.append(f"download: não é imagem ({content_type.split(';')[0]})")
            return None
        if len(response.content) > MAX_IMAGE_BYTES:
            self.errors.append("download: imagem grande demais")
            return None
        return response.content, content_type

    def save(self, post: Post) -> str | None:
        if not post.image_url:
            return None
        try:
            downloaded = self._download(post.image_url)
            if downloaded is None:
                return None
            content, content_type = downloaded
            path = image_path(post, content_type)
            response = self._client.post(
                f"{self._url}/storage/v1/object/{self._bucket}/{path}",
                headers={**self._headers, "Content-Type": content_type, "x-upsert": "true"},
                content=content,
            )
        except httpx.HTTPError as error:
            self.errors.append(f"rede: {type(error).__name__}")
            return None
        if response.status_code not in (200, 201):
            self.errors.append(f"upload: HTTP {response.status_code} {_message(response)}")
            return None
        return path

    def link(self, path: str) -> str | None:
        try:
            response = self._client.post(
                f"{self._url}/storage/v1/object/sign/{self._bucket}/{path}",
                headers=self._headers,
                json={"expiresIn": LINK_SECONDS},
            )
        except httpx.HTTPError as error:
            self.errors.append(f"link: {type(error).__name__}")
            return None
        if response.status_code != 200:
            self.errors.append(f"link: HTTP {response.status_code} {_message(response)}")
            return None
        signed = response.json().get("signedURL")
        return f"{self._url}/storage/v1{signed}" if signed else None

    def check(self) -> str | None:
        """Confere chave e bucket antes de coletar. Devolve o problema, ou `None`."""
        try:
            response = self._client.get(
                f"{self._url}/storage/v1/bucket/{self._bucket}", headers=self._headers
            )
        except httpx.HTTPError as error:
            return f"Storage inacessível ({type(error).__name__})"
        if response.status_code != 200:
            return f"bucket '{self._bucket}': HTTP {response.status_code} {_message(response)}"
        if response.json().get("public"):
            return f"o bucket '{self._bucket}' está público; ele precisa ser privado"
        return None

    def round_trip(self) -> str | None:
        """Grava, gera link, lê pelo link e apaga um arquivo de teste. Devolve o problema."""
        path = "_check/conexao.txt"
        target = f"{self._url}/storage/v1/object/{self._bucket}/{path}"
        try:
            upload = self._client.post(
                target,
                headers={**self._headers, "Content-Type": "text/plain", "x-upsert": "true"},
                content=b"ok",
            )
            if upload.status_code not in (200, 201):
                return f"gravar: HTTP {upload.status_code} {_message(upload)}"
            link = self.link(path)
            if link is None:
                return self.errors[-1] if self.errors else "link: sem URL"
            read = self._client.get(link)
            if read.status_code != 200 or read.content != b"ok":
                return f"ler pelo link: HTTP {read.status_code}"
            delete = self._client.delete(target, headers=self._headers)
            if delete.status_code != 200:
                return f"apagar: HTTP {delete.status_code} {_message(delete)}"
        except httpx.HTTPError as error:
            return f"rede: {type(error).__name__}"
        return None


def _message(response: httpx.Response) -> str:
    try:
        body: Any = response.json()
    except ValueError:
        return ""
    return str(body.get("message", "")) if isinstance(body, dict) else ""
