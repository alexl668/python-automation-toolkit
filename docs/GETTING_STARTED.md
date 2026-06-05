# Getting Started — Python Automation Toolkit

## Installation

```bash
# 1. Clone or download the toolkit
cd python-automation-toolkit

# 2. Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # macOS/Linux
# venv\Scripts\activate   # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

## Quick Examples

### Web Scraping — Extract SEO Data
```bash
python scripts/web_scraping/seo_extractor.py --url "https://example.com"
```

### Data Processing — Transform CSV
```bash
python scripts/data_processing/csv_transformer.py \
  --input data.csv \
  --filter "status=active" \
  --columns "name,email,created_at" \
  --output results.csv
```

### Finance — Track Crypto Prices
```bash
python scripts/finance_crypto/crypto_alerter.py \
  --coins bitcoin,ethereum,solana \
  --alert-bitcoin 50000 \
  --alert-ethereum 3000
```

### File System — Find Duplicates
```bash
python scripts/file_system/duplicate_finder.py \
  --path ~/Downloads \
  --action report
```

## Configuration

Some scripts support config files. Create a `config.yaml` in the toolkit root:

```yaml
# Example config
email:
  smtp_server: smtp.gmail.com
  smtp_port: 587
  username: your@email.com
  # Use app passwords, not your real password

monitoring:
  check_interval: 300  # seconds
  alert_email: alerts@yourdomain.com

api_endpoints:
  - name: "Production API"
    url: "https://api.example.com/health"
    method: GET
    timeout: 10
```

## Script Categories

### 🕷️ Web Scraping
| Script | Description |
|--------|-------------|
| price_monitor.py | Track product prices over time |
| job_aggregator.py | Scrape job listings from career pages |
| seo_extractor.py | Extract SEO metadata from URLs |
| contact_finder.py | Find emails and phone numbers |
| sitemap_crawler.py | Parse and validate XML sitemaps |

### 📊 Data Processing
| Script | Description |
|--------|-------------|
| csv_transformer.py | Transform, filter, merge CSV files |
| json_pipeline.py | Process JSON data with pipelines |
| log_analyzer.py | Parse and analyze log files |
| data_validator.py | Validate data against schemas |
| format_converter.py | Convert between data formats |

### 📧 Email & Communication
| Script | Description |
|--------|-------------|
| email_sender.py | Send templated bulk emails |
| email_analyzer.py | Analyze inbox statistics |
| webhook_server.py | Receive and log webhooks |

### 🌐 API & Web Services
| Script | Description |
|--------|-------------|
| api_monitor.py | Monitor API health and uptime |

### 📁 File & System
| Script | Description |
|--------|-------------|
| file_organizer.py | Auto-organize files by type/date |
| duplicate_finder.py | Find and manage duplicate files |
| backup_system.py | Automated incremental backups |

### 💰 Finance & Crypto
| Script | Description |
|--------|-------------|
| expense_tracker.py | CLI expense tracking with reports |
| invoice_generator.py | Generate PDF invoices |
| crypto_alerter.py | Monitor crypto price alerts |

## Tips

- Use `--help` on any script to see all options
- Most scripts support `--dry-run` to preview changes
- Output files are timestamped to avoid overwrites
- Check `docs/` folder for detailed documentation per script

## Support

Email: luozhixiang6688@gmail.com

## License

MIT License — Free to use in personal and commercial projects.
