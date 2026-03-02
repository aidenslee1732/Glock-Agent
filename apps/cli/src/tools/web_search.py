"""Web Search Tool - Search the web using DuckDuckGo.

Provides web search capabilities without API keys:
- DuckDuckGo HTML search (free, no API key)
- Domain filtering
- Result parsing and formatting
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Check for aiohttp
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

# Check for BeautifulSoup
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False


@dataclass
class SearchResult:
    """A single search result."""

    title: str
    url: str
    snippet: str
    domain: str = ""

    def __post_init__(self):
        """Extract domain from URL."""
        if self.url and not self.domain:
            try:
                self.domain = urllib.parse.urlparse(self.url).netloc
            except Exception:
                pass

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary."""
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "domain": self.domain,
        }

    def to_markdown(self) -> str:
        """Format as markdown."""
        return f"- [{self.title}]({self.url})\n  {self.snippet}"


@dataclass
class SearchResponse:
    """Response from a web search."""

    query: str
    results: list[SearchResult]
    status: str = "success"
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def result_count(self) -> int:
        """Number of results."""
        return len(self.results)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "query": self.query,
            "status": self.status,
            "results": [r.to_dict() for r in self.results],
            "result_count": self.result_count,
            "error": self.error,
            "metadata": self.metadata,
        }

    def to_markdown(self) -> str:
        """Format results as markdown."""
        if self.error:
            return f"Search error: {self.error}"

        if not self.results:
            return f"No results found for: {self.query}"

        lines = [f"## Search Results: {self.query}", ""]
        for result in self.results:
            lines.append(result.to_markdown())

        return "\n".join(lines)


class WebSearchTool:
    """Web search tool using DuckDuckGo.

    Provides web search capabilities without requiring API keys.
    Uses DuckDuckGo's HTML search endpoint.
    """

    def __init__(
        self,
        timeout: float = 30.0,
        max_results: int = 10,
    ):
        """Initialize web search tool.

        Args:
            timeout: Request timeout in seconds
            max_results: Default maximum results
        """
        self.timeout = timeout
        self.max_results = max_results

        # Check dependencies
        if not AIOHTTP_AVAILABLE:
            logger.warning("aiohttp not available - web search disabled")

    @property
    def available(self) -> bool:
        """Check if web search is available."""
        return AIOHTTP_AVAILABLE

    async def search(
        self,
        query: str,
        num_results: int = None,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
    ) -> SearchResponse:
        """Search the web.

        Args:
            query: Search query
            num_results: Maximum number of results
            allowed_domains: Only include results from these domains
            blocked_domains: Exclude results from these domains

        Returns:
            SearchResponse with results
        """
        if not AIOHTTP_AVAILABLE:
            return SearchResponse(
                query=query,
                results=[],
                status="error",
                error="aiohttp not available - install with: pip install aiohttp",
            )

        num_results = num_results or self.max_results

        # Use DuckDuckGo HTML search
        search_url = "https://html.duckduckgo.com/html/"

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; GlockBot/1.0)",
            "Accept": "text/html",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    search_url,
                    data={"q": query},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as response:
                    if response.status != 200:
                        return SearchResponse(
                            query=query,
                            results=[],
                            status="error",
                            error=f"Search failed with status {response.status}",
                        )

                    html_content = await response.text()

        except aiohttp.ClientError as e:
            return SearchResponse(
                query=query,
                results=[],
                status="error",
                error=str(e),
            )
        except asyncio.TimeoutError:
            return SearchResponse(
                query=query,
                results=[],
                status="error",
                error="Search request timed out",
            )

        # Parse results (get more than needed for filtering)
        all_results = self._parse_duckduckgo_results(html_content, num_results * 3)

        # Apply domain filters
        filtered_results = self._filter_by_domain(
            all_results,
            allowed_domains,
            blocked_domains,
            num_results,
        )

        return SearchResponse(
            query=query,
            results=filtered_results,
            status="success",
            metadata={
                "engine": "duckduckgo",
                "raw_results": len(all_results),
                "filtered_results": len(filtered_results),
            },
        )

    def _parse_duckduckgo_results(
        self,
        html_content: str,
        max_results: int,
    ) -> list[SearchResult]:
        """Parse DuckDuckGo HTML search results."""
        results = []

        if BS4_AVAILABLE:
            soup = BeautifulSoup(html_content, "html.parser")

            # Find result divs
            for result in soup.select(".result"):
                if len(results) >= max_results:
                    break

                # Get title and URL
                title_elem = result.select_one(".result__title a")
                snippet_elem = result.select_one(".result__snippet")

                if title_elem:
                    title = title_elem.get_text(strip=True)
                    url = title_elem.get("href", "")

                    # DuckDuckGo uses redirect URLs, extract actual URL
                    if "uddg=" in url:
                        parsed = urllib.parse.parse_qs(
                            urllib.parse.urlparse(url).query
                        )
                        url = parsed.get("uddg", [url])[0]

                    snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

                    results.append(SearchResult(
                        title=title,
                        url=url,
                        snippet=snippet,
                    ))
        else:
            # Fallback regex parsing
            pattern = r'class="result__title"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>'
            for match in re.finditer(pattern, html_content, re.DOTALL):
                if len(results) >= max_results:
                    break

                url, title = match.groups()

                # Extract actual URL from DuckDuckGo redirect
                if "uddg=" in url:
                    parsed = urllib.parse.parse_qs(
                        urllib.parse.urlparse(url).query
                    )
                    url = parsed.get("uddg", [url])[0]

                results.append(SearchResult(
                    title=html.unescape(title.strip()),
                    url=url,
                    snippet="",
                ))

        return results

    def _filter_by_domain(
        self,
        results: list[SearchResult],
        allowed_domains: Optional[list[str]],
        blocked_domains: Optional[list[str]],
        max_results: int,
    ) -> list[SearchResult]:
        """Filter results by domain."""
        filtered = []

        for result in results:
            domain = result.domain.lower()

            # Check blocked domains
            if blocked_domains:
                blocked = False
                for blocked_domain in blocked_domains:
                    if blocked_domain.lower() in domain:
                        blocked = True
                        break
                if blocked:
                    continue

            # Check allowed domains
            if allowed_domains:
                allowed = False
                for allowed_domain in allowed_domains:
                    if allowed_domain.lower() in domain:
                        allowed = True
                        break
                if not allowed:
                    continue

            filtered.append(result)
            if len(filtered) >= max_results:
                break

        return filtered


# Convenience function for direct use
async def web_search(
    query: str,
    num_results: int = 10,
    allowed_domains: Optional[list[str]] = None,
    blocked_domains: Optional[list[str]] = None,
) -> SearchResponse:
    """Search the web using DuckDuckGo.

    Args:
        query: Search query
        num_results: Maximum number of results
        allowed_domains: Only include results from these domains
        blocked_domains: Exclude results from these domains

    Returns:
        SearchResponse with results
    """
    tool = WebSearchTool()
    return await tool.search(
        query=query,
        num_results=num_results,
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
    )


# Tool handler for broker integration
async def web_search_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handler for web search tool.

    Args:
        args: Tool arguments

    Returns:
        Tool result dictionary
    """
    query = args.get("query")
    if not query:
        return {
            "status": "error",
            "error": "Query is required",
        }

    response = await web_search(
        query=query,
        num_results=args.get("num_results", 10),
        allowed_domains=args.get("allowed_domains"),
        blocked_domains=args.get("blocked_domains"),
    )

    return response.to_dict()
