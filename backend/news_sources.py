"""RSS feed source configuration for News Desk.

To disable a feed without code changes, set "enabled": False.
Weight (0.8–1.2) scales how much the source's items' impact scores are boosted/dampened.
"""

NEWS_SOURCES = [
    {
        "name": "Zerodha Pulse",
        "url": "http://pulse.zerodha.com/feed.php",
        "enabled": True,
        "weight": 1.0,
    },
    {
        "name": "Bloomberg Markets",
        "url": "https://feeds.bloomberg.com/markets/news.rss",
        "enabled": True,
        "weight": 1.1,
    },
    {
        "name": "India News Network",
        "url": "https://www.indianewsnetwork.com/rss.en.business.xml",
        "enabled": True,
        "weight": 0.9,
    },
    {
        "name": "Tickertape Blog",
        "url": "https://www.tickertape.in/blog/feed",
        "enabled": True,
        "weight": 0.8,
    },
]
