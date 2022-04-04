from dataclasses import dataclass
import aiofiles.os
import yaml
from datetime import datetime, timedelta
import urllib
from typing import List, Optional, Tuple
import aiohttp
import os
import sys
import asyncio
from bs4 import BeautifulSoup
import feedendum


@dataclass
class Article:
    title: str
    link: str
    slug: str
    published_time: datetime
    category: str
    modified_time: Optional[datetime]


def parse_index(html: str) -> Tuple[List[Article], Optional[str]]:
    soup = BeautifulSoup(html, 'html.parser')
    # get next url path
    next_url = None
    n = soup.find('link', rel='next')
    if n:
        next_url = n['href']
    # find all articles
    articles = []
    for a in soup.find_all('article'):
        title = a.find('h2')
        link = title.find('a')['href']
        *_, category = urllib.parse.urlparse(
            a.find('a', rel='category tag')['href']
        ).path.rstrip('/').split('/')
        *_, slug = urllib.parse.urlparse(
            link
        ).path.rstrip('/').split('/')
        articles.append(Article(
            title=''.join(title.strings).strip(),
            link=link,
            slug=slug,
            published_time=datetime.fromisoformat(a.find('time')['datetime']),
            category=category,
            modified_time=None,
        ))
    return (articles, next_url)


async def parse_article(html: str, article: Article) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for l in soup.find_all('a'):
        del l['rel']
        del l['target']
    mt = soup.find('meta', property='article:modified_time')
    if mt:
        article.modified_time = datetime.fromisoformat(
            mt['content']).astimezone(article.published_time.tzinfo)
    a = soup.find('article').find('section').encode()
    proc = await asyncio.create_subprocess_exec('pandoc', '-s', '-f', 'html', '-t', 'markdown_strict',
                                                stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate(a)
    return stdout.decode('utf-8')


async def fetch_index_worker(session: aiohttp.ClientSession, queue: asyncio.Queue[Article], oldest: datetime) -> List[Article]:
    next_url = 'https://www.whitehouse.gov/briefing-room/'

    fetched_articles = []
    while True:
        async with session.get(next_url) as response:
            print('Fetching', next_url)
            html = await response.text()
            articles, next_url = parse_index(html)
            fetch_next = next_url is not None
            for article in articles:
                if article.published_time > oldest:
                    await queue.put(article)
                    fetched_articles.append(article)
                else:
                    fetch_next = False
                    break
            if not fetch_next:
                break
    return fetched_articles


async def fetch_article_worker(session: aiohttp.ClientSession, queue: asyncio.Queue[Article]):
    while True:
        a = await queue.get()
        try:
            dir = f'{a.category}/{a.published_time.year}-{a.published_time.month:02d}'
            filename = f'{dir}/{a.published_time.year}-{a.published_time.month:02d}-{a.published_time.day:02d}-{a.slug}.md'

            async with session.get(a.link) as response:
                html = await response.text()
                markdown = await parse_article(html, a)
                await aiofiles.os.makedirs(dir, exist_ok=True)
                async with aiofiles.open(filename, 'w') as f:
                    front_matter = {
                        'title': a.title,
                        'tags': a.category,
                        'source_url': a.link,
                        'date': f'{a.published_time.year}-{a.published_time.month:02d}-{a.published_time.day:02d}',
                        'published_time': a.published_time,
                    }
                    if a.modified_time:
                        front_matter['modified_time'] = a.modified_time
                    await f.write(f'---\n{yaml.dump(front_matter)}---\n \n')
                    await f.write(markdown)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise SystemExit(str(e))
        except:
            raise SystemExit('unexpected error')
        queue.task_done()


async def main():

    prev_items = []
    try:
        async with aiofiles.open('rss.xml', 'r') as f:
            feed = feedendum.from_rss_text(await f.read())
            prev_items = feed.items
    except FileNotFoundError:
        pass

    prev_date = max((i.update for i in prev_items),
                    default=datetime.fromisoformat("2020-01-01T00:00:00+00:00"))
    prev_date -= timedelta(days=7)

    async with aiohttp.ClientSession() as session:
        queue = asyncio.Queue(16)
        tasks = [
            asyncio.create_task(
                fetch_article_worker(session, queue)
            )
            for _ in range(4)
        ]
        articles = await fetch_index_worker(session, queue, prev_date)
        await queue.join()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    items = [
        feedendum.feed.FeedItem(
            title=a.title,
            url=a.link,
            id=a.link,
            update=a.published_time,
            categories=[a.category],
        )
        for a in articles
    ]
    seen = set()
    for i in items:
        seen.add(i.url)
    for i in prev_items:
        if i.url in seen:
            continue
        seen.add(i.url)
        items.append(feedendum.feed.FeedItem(
            title=i.title,
            url=i.url,
            id=i.id,
            update=i.update,
            categories=i.categories[:],
        ))
    if len(prev_items) != len(items):
        items = sorted(items, key=lambda i: i.update, reverse=True)[:30]
        async with aiofiles.open('rss.xml', 'w') as f:
            feed = feedendum.feed.Feed(
                title='The White House Briefing Room',
                description='The White House Briefing Room',
                url='https://www.whitehouse.gov/briefing-room/',
                update=max(map(lambda i: i.update, items), default=datetime.utcnow()),
                items=items,
            )
            rss = BeautifulSoup(feedendum.to_rss_string(feed), 'xml')
            await f.write(rss.prettify())

if __name__ == '__main__':
    os.chdir(sys.argv[1])
    asyncio.new_event_loop().run_until_complete(main())
