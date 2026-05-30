#!/usr/bin/env python3
"""
Dev Deck Browser Inspector
A zero-dependency tool for agents to fetch and read web pages.
Converts HTML into clean, LLM-readable Markdown structure.
"""
import argparse
import urllib.request
import urllib.error
from html.parser import HTMLParser

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_blocks = []
        self.in_script = False
        self.in_style = False
        self.current_tag = ""

    def handle_starttag(self, tag, attrs):
        self.current_tag = tag
        if tag == "script":
            self.in_script = True
        elif tag == "style":
            self.in_style = True
        elif tag in ["h1", "h2", "h3", "h4", "h5", "h6"]:
            level = tag[1]
            self.text_blocks.append(f"\n\n{'#' * int(level)} ")
        elif tag == "p":
            self.text_blocks.append("\n\n")
        elif tag == "a":
            href = next((v for k, v in attrs if k == "href"), None)
            if href:
                self.text_blocks.append(f" [LINK: {href}] ")
        elif tag == "button":
            self.text_blocks.append(" [BUTTON] ")
        elif tag == "li":
            self.text_blocks.append("\n- ")

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
        elif tag == "style":
            self.in_style = False
        elif tag in ["h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section"]:
            self.text_blocks.append("\n")

    def handle_data(self, data):
        if not self.in_script and not self.in_style:
            clean = data.strip()
            if clean:
                self.text_blocks.append(clean + " ")

    def get_text(self):
        # Clean up excessive newlines
        import re
        raw = "".join(self.text_blocks)
        return re.sub(r'\n{3,}', '\n\n', raw).strip()

def dump_url(url: str):
    if not url.startswith("http"):
        url = "https://" + url
        
    print(f"Fetching {url}...")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    req = urllib.request.Request(url, headers=headers)
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8', errors='ignore')
            
        parser = TextExtractor()
        parser.feed(html)
        text = parser.get_text()
        
        print("\n=== BROWSER DUMP ===\n")
        # Truncate to save context window if massive
        if len(text) > 15000:
            print(text[:15000])
            print("\n... [CONTENT TRUNCATED FOR CONTEXT SIZE] ...")
        else:
            print(text)
            
    except urllib.error.URLError as e:
        print(f"Error fetching URL: {e}")
    except Exception as e:
        print(f"Failed to parse: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dev Deck Browser Inspector")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    dump_parser = subparsers.add_parser("dump", help="Fetch URL and extract text")
    dump_parser.add_argument("url", type=str)
    
    args = parser.parse_args()
    
    if args.command == "dump":
        dump_url(args.url)
