"""
ChatGPTMail / GPTMail provider (web2api)

站点: https://mail.chatgpt.org.uk
协议已从旧版 window.__BROWSER_AUTH 迁移到网页版流程:

1) GET  /api/domains/public?view=bootstrap   拉公开域名
2) 本地生成 prefix@domain
3) POST /api/inbox-token  {"email": "..."}   拿 x-inbox-token
4) GET  /api/emails?email=...               Header: x-inbox-token
5) GET  /api/email/{id}?email=...           Header: x-inbox-token

不需要浏览器，不需要 API Key。
"""

from __future__ import annotations

import logging
import random
import string
import time
from typing import Dict, List, Optional

from curl_cffi import requests as curl_requests

from .base import InboxEmail, TempEmail, TempMailClient
from .utils import EmailFetchError, EmailGenerateError, retry

logger = logging.getLogger("chatgptmail-2api")

BASE_URL = "https://mail.chatgpt.org.uk"


class ChatGPTMailClient(TempMailClient):
    def __init__(self, proxy: Optional[str] = None, timeout: int = 20) -> None:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        self.session = curl_requests.Session(impersonate="chrome136", proxies=proxies)
        self.timeout = timeout
        # 兼容旧字段，但真正按邮箱隔离 token，避免多线程覆盖
        self._inbox_token: Optional[str] = None
        self._email: Optional[str] = None
        self._token_by_email: Dict[str, str] = {}
        self._domains_cache: List[str] = []
        self._domains_cached_at = 0.0

    @property
    def provider_name(self) -> str:
        return "chatgptmail"

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {
            "Accept": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
        }
        if extra:
            h.update(extra)
        return h

    def _auth_headers(self, token: Optional[str] = None) -> Dict[str, str]:
        tok = token or self._inbox_token
        extra = {"x-inbox-token": tok} if tok else {}
        return self._headers(extra)

    @retry(max_attempts=3, backoff_factor=1.5, exceptions=(Exception,))
    def _fetch_public_domains(self, force: bool = False) -> List[str]:
        now = time.time()
        if not force and self._domains_cache and now - self._domains_cached_at < 300:
            return self._domains_cache
        r = self.session.get(
            f"{BASE_URL}/api/domains/public",
            params={"view": "bootstrap"},
            headers=self._headers({"cache-control": "no-cache", "pragma": "no-cache"}),
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise EmailGenerateError(f"domains/public failed: {data}")
        domains = []
        for item in (data.get("data") or {}).get("domains") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("domain_name") or item.get("domain")
            if name and item.get("is_active", 1):
                domains.append(str(name).strip().lower())
        if not domains:
            raise EmailGenerateError("domains/public returned empty list")
        self._domains_cache = domains
        self._domains_cached_at = now
        return domains

    def _rand_prefix(self, n: int = 10) -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

    @retry(max_attempts=3, backoff_factor=1.5, exceptions=(Exception,))
    def _issue_inbox_token(self, email: str) -> str:
        r = self.session.post(
            f"{BASE_URL}/api/inbox-token",
            headers=self._headers({"content-type": "application/json"}),
            json={"email": email},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        token = (
            ((data.get("auth") or {}).get("token"))
            or ((data.get("data") or {}).get("token"))
            or ""
        )
        if not token:
            raise EmailGenerateError(f"inbox-token missing token: {data}")
        email_l = email.strip().lower()
        self._token_by_email[email_l] = token
        self._inbox_token = token
        self._email = email_l
        return token

    @retry(max_attempts=3, backoff_factor=1.5, exceptions=(Exception,))
    def generate_email(self, duration_minutes: int = 10, domain: Optional[str] = None) -> TempEmail:
        """网页版协议：公开域名 + 本地前缀 + inbox-token。"""
        domains = self._fetch_public_domains()
        chosen = (domain or random.choice(domains)).strip().lower()
        if chosen not in domains:
            try:
                email = f"{self._rand_prefix()}@{chosen}"
                self._issue_inbox_token(email)
            except Exception:
                chosen = random.choice(domains)
                email = f"{self._rand_prefix()}@{chosen}"
                self._issue_inbox_token(email)
        else:
            email = f"{self._rand_prefix()}@{chosen}"
            self._issue_inbox_token(email)

        logger.info("chatgptmail web2api email=%s", email)
        return TempEmail(
            address=email,
            provider=self.provider_name,
            duration_minutes=duration_minutes,
            raw={"domain": chosen, "auth_token": (self._inbox_token or "")[:16] + "..."},
        )

    def _ensure_token(self, address: str) -> str:
        email_l = (address or "").strip().lower()
        tok = self._token_by_email.get(email_l)
        if tok:
            self._inbox_token = tok
            self._email = email_l
            return tok
        return self._issue_inbox_token(address)

    @retry(max_attempts=2, backoff_factor=1.0, exceptions=(Exception,))
    def list_emails(self, address: str) -> List[InboxEmail]:
        token = self._ensure_token(address)
        r = self.session.get(
            f"{BASE_URL}/api/emails",
            params={"email": address},
            headers=self._auth_headers(token),
            timeout=self.timeout,
        )
        if r.status_code in (401, 403):
            token = self._issue_inbox_token(address)
            r = self.session.get(
                f"{BASE_URL}/api/emails",
                params={"email": address},
                headers=self._auth_headers(token),
                timeout=self.timeout,
            )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise EmailFetchError(f"list emails failed: {data}")
        emails = (data.get("data") or {}).get("emails") or []
        result: List[InboxEmail] = []
        for e in emails:
            # GPTMail 网页协议字段:
            # list: content / html_content
            # detail: html_content / content / raw_content
            body_html = (
                e.get("html_content")
                or e.get("html")
                or e.get("body_html")
                or e.get("body")
                or e.get("content_html")
                or ""
            )
            body_text = (
                e.get("content")
                or e.get("text")
                or e.get("body_text")
                or e.get("content_text")
                or e.get("snippet")
                or e.get("preview")
                or ""
            )
            subject = e.get("subject") or ""
            if subject and subject not in str(body_text):
                body_text = f"{subject}\n{body_text}"
            result.append(
                InboxEmail(
                    id=str(e.get("id", "")),
                    provider=self.provider_name,
                    subject=subject,
                    from_email=e.get("from_address") or e.get("from") or e.get("sender"),
                    body_html=body_html,
                    body_text=body_text,
                    received_at=str(
                        e.get("date") or e.get("received_at") or e.get("timestamp") or ""
                    ),
                    raw=e,
                )
            )
        return result

    @retry(max_attempts=2, backoff_factor=1.0, exceptions=(Exception,))
    def get_email_detail(self, address: str, email_id: str) -> Optional[InboxEmail]:
        """兼容 list 调用签名: get_email_detail(address, email_id)。"""
        token = self._ensure_token(address)
        r = self.session.get(
            f"{BASE_URL}/api/email/{email_id}",
            params={"email": address, "include_raw": "0"},
            headers=self._auth_headers(token),
            timeout=self.timeout,
        )
        if r.status_code in (401, 403):
            token = self._issue_inbox_token(address)
            r = self.session.get(
                f"{BASE_URL}/api/email/{email_id}",
                params={"email": address, "include_raw": "0"},
                headers=self._auth_headers(token),
                timeout=self.timeout,
            )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise EmailFetchError(f"email detail failed: {data}")
        email_data = data.get("data") or {}
        body_html = (
            email_data.get("html_content")
            or email_data.get("html")
            or email_data.get("body_html")
            or email_data.get("body")
            or email_data.get("content_html")
            or ""
        )
        body_text = (
            email_data.get("content")
            or email_data.get("text")
            or email_data.get("body_text")
            or email_data.get("content_text")
            or email_data.get("raw_content")
            or email_data.get("snippet")
            or email_data.get("preview")
            or ""
        )
        subject = email_data.get("subject") or ""
        if subject and subject not in str(body_text):
            body_text = f"{subject}\n{body_text}"
        return InboxEmail(
            id=str(email_data.get("id", email_id)),
            provider=self.provider_name,
            subject=subject,
            from_email=email_data.get("from_address")
            or email_data.get("from")
            or email_data.get("sender"),
            body_html=body_html,
            body_text=body_text,
            received_at=str(email_data.get("date") or email_data.get("received_at") or ""),
            raw=email_data,
        )
