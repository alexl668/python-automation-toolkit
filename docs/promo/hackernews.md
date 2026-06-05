# Hacker News — Show HN

## Title:
Show HN: Python Automation Toolkit – 50+ production-ready scripts for web scraping, data processing, and more

## Body:
I've been writing Python automation scripts for years and kept reusing the same patterns. Finally packaged them into an open-source toolkit.

**What's inside:**
- Web scraping (price monitors, SEO extractors, job aggregators)
- Data processing (CSV/JSON/log transformers, validators)
- Email automation (bulk senders with Jinja2 templates)
- API services (health monitors, FastAPI webhook receivers)
- File management (duplicate finder, backup system)
- Finance (expense tracker with SQLite, crypto alerter)

**Design decisions:**
- Every script is standalone — no framework lock-in
- CLI-first with Click + Rich for beautiful terminal output
- SQLite for local data persistence (no external DB needed)
- MIT License

**Quick example:**
```
$ python scripts/web_scraping/price_monitor.py --url "https://example.com"
$ python scripts/data_processing/csv_transformer.py --input data.csv --filter "status=active"
```

GitHub: https://github.com/alexl668/python-automation-toolkit

Feedback on architecture and script design welcome.
