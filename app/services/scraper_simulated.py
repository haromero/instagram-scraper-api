"""Backend simulado (determinista) para desarrollo y demostración.

Cada función produce datos reproducibles a partir de una semilla, de modo que
la API sea demostrable sin depender de Instagram ni de credenciales externas.
Es el backend por defecto (``IG_SCRAPER_BACKEND=simulated``).
"""

import hashlib
import random
from datetime import datetime, timedelta, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rng(seed: str) -> random.Random:
    return random.Random(int(hashlib.sha256(seed.encode()).hexdigest(), 16))


def _shortcode(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:9].upper()


def user_summary(username: str) -> dict:
    r = _rng("user:" + username)
    return {
        "username": username,
        "user_id": str(r.randint(10**8, 10**10)),
        "full_name": username.replace("_", " ").replace(".", " ").title(),
        "is_verified": r.random() < 0.2,
        "is_private": r.random() < 0.1,
        "profile_pic_url": f"https://cdn.example.com/{username}/avatar.jpg",
    }


class SimulatedScraper:
    """Genera datos de ejemplo con la forma exacta del contrato público."""

    def get_profile(self, username: str) -> dict:
        r = _rng("profile:" + username)
        summary = user_summary(username)
        return {
            "username": username,
            "user_id": summary["user_id"],
            "full_name": summary["full_name"],
            "biography": f"Perfil simulado de @{username}",
            "is_private": summary["is_private"],
            "is_verified": summary["is_verified"],
            "followers_count": r.randint(100, 10_000_000),
            "following_count": r.randint(10, 5_000),
            "posts_count": r.randint(0, 5_000),
            "profile_pic_url": summary["profile_pic_url"],
            "external_url": r.choice([None, f"https://{username}.example.com"]),
            "scraped_at": _now(),
        }

    def get_posts(self, username: str, count: int = 120) -> list[dict]:
        r = _rng("posts:" + username)
        owner = user_summary(username)
        base = _now()
        posts: list[dict] = []
        for i in range(count):
            shortcode = _shortcode(f"{username}:{i}")
            media_type = r.choice(["image", "video", "carousel", "reel"])
            media_count = 3 if media_type == "carousel" else 1
            posts.append(
                {
                    "id": str(r.randint(10**9, 10**10)),
                    "shortcode": shortcode,
                    "type": media_type,
                    "caption": f"Publicación simulada #{i + 1} de @{username}",
                    "like_count": r.randint(0, 100_000),
                    "comment_count": r.randint(0, 2_000),
                    "taken_at": base - timedelta(hours=i * 6),
                    "media_urls": [
                        f"https://cdn.example.com/{username}/{shortcode}_{j}.jpg"
                        for j in range(media_count)
                    ],
                    "thumbnail_url": f"https://cdn.example.com/{username}/{shortcode}_thumb.jpg",
                    "owner": owner,
                    "scraped_at": base,
                }
            )
        return posts

    def get_post(self, shortcode: str) -> dict:
        r = _rng("post:" + shortcode)
        base = _now()
        media_type = r.choice(["image", "video", "carousel", "reel"])
        media_count = 3 if media_type == "carousel" else 1
        username = f"creator_{r.randint(1000, 9999)}"
        return {
            "id": str(r.randint(10**9, 10**10)),
            "shortcode": shortcode,
            "type": media_type,
            "caption": f"Publicación simulada {shortcode}",
            "like_count": r.randint(0, 100_000),
            "comment_count": r.randint(0, 2_000),
            "taken_at": base - timedelta(hours=r.randint(1, 500)),
            "media_urls": [
                f"https://cdn.example.com/posts/{shortcode}_{j}.jpg"
                for j in range(media_count)
            ],
            "thumbnail_url": f"https://cdn.example.com/posts/{shortcode}_thumb.jpg",
            "owner": user_summary(username),
            "scraped_at": base,
        }

    def get_comments(self, shortcode: str, count: int = 120) -> list[dict]:
        r = _rng("comments:" + shortcode)
        base = _now()
        comments: list[dict] = []
        for i in range(count):
            username = f"user_{r.randint(1000, 9999)}"
            comments.append(
                {
                    "id": str(r.randint(10**9, 10**10)),
                    "text": f"Comentario simulado #{i + 1}",
                    "author": user_summary(username),
                    "like_count": r.randint(0, 500),
                    "replies_count": r.randint(0, 5),
                    "created_at": base - timedelta(minutes=i * 10),
                }
            )
        return comments

    def get_users(self, seed: str, count: int = 120) -> list[dict]:
        r = _rng(seed)
        seen: set[str] = set()
        users: list[dict] = []
        while len(users) < count:
            username = f"user_{r.randint(10000, 99999)}"
            if username in seen:
                continue
            seen.add(username)
            users.append(user_summary(username))
        return users

    def get_likes(self, shortcode: str, count: int = 120) -> list[dict]:
        return self.get_users("likes:" + shortcode, count=count)

    def get_followers(self, username: str, count: int = 120) -> list[dict]:
        return self.get_users("followers:" + username, count=count)

    def get_following(self, username: str, count: int = 120) -> list[dict]:
        return self.get_users("following:" + username, count=count)

    def get_stories(self, username: str, count: int = 10) -> list[dict]:
        r = _rng("stories:" + username)
        base = _now()
        return [
            {
                "id": str(r.randint(10**9, 10**10)),
                "media_type": r.choice(["image", "video"]),
                "media_url": f"https://cdn.example.com/{username}/story_{i}.jpg",
                "thumbnail_url": f"https://cdn.example.com/{username}/story_{i}_thumb.jpg",
                "taken_at": base - timedelta(hours=i),
                "expires_at": base + timedelta(hours=24 - i),
            }
            for i in range(count)
        ]

    def get_highlights(self, username: str, count: int = 10) -> list[dict]:
        r = _rng("highlights:" + username)
        base = _now()
        return [
            {
                "id": f"highlight:{r.randint(10**9, 10**10)}",
                "title": f"Highlight {i + 1}",
                "cover_url": f"https://cdn.example.com/{username}/hl_{i}.jpg",
                "item_count": r.randint(1, 30),
                "updated_at": base - timedelta(days=i),
            }
            for i in range(count)
        ]

    def get_hashtag_posts(
        self, hashtag: str, mode: str = "recent", count: int = 120
    ) -> list[dict]:
        r = _rng(f"hashtag_posts:{hashtag}:{mode}")
        base = _now()
        posts: list[dict] = []
        for i in range(count):
            username = f"creator_{r.randint(1000, 9999)}"
            shortcode = _shortcode(f"{hashtag}:{mode}:{i}")
            media_type = r.choice(["image", "video", "carousel"])
            media_count = 3 if media_type == "carousel" else 1
            posts.append(
                {
                    "id": str(r.randint(10**9, 10**10)),
                    "shortcode": shortcode,
                    "type": media_type,
                    "caption": f"#{hashtag} post #{i + 1}",
                    "like_count": r.randint(0, 100_000),
                    "comment_count": r.randint(0, 2_000),
                    "taken_at": base - timedelta(hours=i * 4),
                    "media_urls": [
                        f"https://cdn.example.com/{hashtag}/{shortcode}_{j}.jpg"
                        for j in range(media_count)
                    ],
                    "thumbnail_url": f"https://cdn.example.com/{hashtag}/{shortcode}_thumb.jpg",
                    "owner": user_summary(username),
                    "scraped_at": base,
                }
            )
        return posts

    def get_hashtag(self, hashtag: str) -> dict:
        r = _rng("hashtag:" + hashtag)
        top = self.get_hashtag_posts(hashtag, mode="top", count=5)
        return {
            "name": hashtag,
            "media_count": r.randint(1_000, 50_000_000),
            "top_posts": [
                {
                    "shortcode": post["shortcode"],
                    "like_count": post["like_count"],
                    "thumbnail_url": post["thumbnail_url"],
                }
                for post in top
            ],
            "scraped_at": _now(),
        }

    def search(
        self, query: str, kind: str = "all", count: int = 120
    ) -> list[dict]:
        r = _rng(f"search:{query}:{kind}")
        results: list[dict] = []
        if kind in ("user", "all"):
            for _ in range(count):
                username = f"{query.lower()}_{r.randint(1, 999)}"
                results.append({"kind": "user", **user_summary(username)})
        if kind in ("hashtag", "all"):
            for _ in range(count):
                results.append(
                    {
                        "kind": "hashtag",
                        "name": f"{query.lower()}{r.randint(1, 99)}",
                        "media_count": r.randint(100, 1_000_000),
                    }
                )
        return results

    def build_result(
        self, job_type: str, target: dict, options: dict
    ) -> list[dict]:
        """Construye el resultado de un job a partir de su tipo y target."""
        limit = min(int(options.get("limit") or 120), 200)
        username = target.get("username") or "unknown"
        shortcode = target.get("shortcode") or "unknown"
        hashtag = target.get("hashtag") or "unknown"
        query = target.get("query") or "unknown"

        if job_type == "profile":
            return [self.get_profile(username)]
        if job_type == "profile_posts":
            return self.get_posts(username, count=limit)
        if job_type == "profile_followers":
            return self.get_followers(username, count=limit)
        if job_type == "profile_following":
            return self.get_following(username, count=limit)
        if job_type == "post_comments":
            return self.get_comments(shortcode, count=limit)
        if job_type == "hashtag_posts":
            return self.get_hashtag_posts(
                hashtag, mode=options.get("mode") or "recent", count=limit
            )
        if job_type == "search":
            return self.search(query, kind=options.get("mode") or "all", count=limit)
        return []
