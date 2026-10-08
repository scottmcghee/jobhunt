"""A request extension that carries a source's own rate cap to the throttled transport.

Some sites ask for a crawl delay in robots.txt but live on hosts nothing else identifies (a
company's own careers domain), so the transport can't know their cap from the URL. Their source
sends it with each request: ``client.get(url, extensions={RATE: 0.2})``. A ``fetch.max_rate``
setting for the group still wins.
"""

RATE = "jobhunt_rate"  # requests per second
