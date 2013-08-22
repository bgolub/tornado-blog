import BeautifulSoup
import functools
import hashlib
import json
import os
import re
import tornado.web
import tornado.wsgi
import unicodedata
import urllib
import uuid
import wsgiref.handlers

from google.appengine.ext import db
from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.appengine.api import users


def administrator(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        user = users.get_current_user()
        if not user:
            if self.request.method == "GET":
                self.redirect(users.create_login_url(self.request.uri))
                return
            raise tornado.web.HTTPError(403)
        elif not users.is_current_user_admin():
            raise tornado.web.HTTPError(403)
        else:
            return method(self, *args, **kwargs)
    return wrapper


class Entry(db.Model):
    author = db.UserProperty()
    title = db.StringProperty(required=True)
    slug = db.StringProperty(required=True)
    body = db.TextProperty(required=True)
    published = db.DateTimeProperty(auto_now_add=True)
    updated = db.DateTimeProperty(auto_now=True)
    tags = db.ListProperty(db.Category)
    hidden = db.BooleanProperty(default=False)


class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        user = users.get_current_user()
        if user:
            user.administrator = users.is_current_user_admin()
        return user

    def get_integer_argument(self, name, default):
        try:
            return int(self.get_argument(name, default))
        except (TypeError, ValueError):
            return default

    def render_string(self, template_name, **kwargs):
        return tornado.web.RequestHandler.render_string(self, template_name,
            users=users, **kwargs)

    def render(self, template_name, **kwargs):
        format = self.get_argument("format", None)
        if "entries" in kwargs and isinstance(kwargs["entries"], db.Query):
            # Force evaluate queries so we know if there are entries before
            # trying to render a feed
            kwargs["entries"] = list(kwargs["entries"])
        if kwargs.get("entries") and format == "atom":
            self.set_header("Content-Type", "application/atom+xml")
            self.set_sup_header()
            template_name = "atom.xml"
        if "entries" in kwargs and format == "json":
            json_entries = [{
                "title": entry.title,
                "slug": entry.slug,
                "body": entry.body,
                "author": entry.author.nickname(),
                "published": entry.published.isoformat(),
                "updated": entry.updated.isoformat(),
                "tags": entry.tags,
                "link": "http://" + self.request.host + "/" + entry.slug,
            } for entry in kwargs["entries"]]
            data = {
                "entries": json_entries,
            }
            if "cursor" in kwargs:
                data["cursor"] = kwargs["cursor"]
            self.set_header("Content-Type", "text/javascript")
            self.write(json.dumps(data, sort_keys=True, indent=4) if 
                self.get_argument("pretty", False) else data)
            return
        return tornado.web.RequestHandler.render(self, template_name, **kwargs)

    def slugify(self, value):
        slug = unicodedata.normalize("NFKD", value).encode(
            "ascii", "ignore")
        slug = re.sub(r"[^\w]+", " ", slug)
        return "-".join(slug.lower().strip().split())

    def generate_sup_id(self, url=None):
        return hashlib.md5(url or self.request.full_url()).hexdigest()[:10]

    def set_sup_header(self, url=None):
        sup_id = self.generate_sup_id(url)
        self.set_header("X-SUP-ID",
            "http://friendfeed.com/api/public-sup.json#" + sup_id) 

    def ping(self):
        # Swallow exceptions when pinging, urlfetch can be unstable and it
        # isn't the end of the world if a ping doesn't make it. Since we don't
        # care about the response, in an ideal world, the urlfetch API would
        # have an option to perform all of this asynchronously and optionally
        # specify a number of retries
        feed = "http://" + self.request.host + "/?format=atom"
        args = urllib.urlencode({
            "name": self.application.settings["blog_title"],
            "url": "http://" + self.request.host + "/",
            "changesURL": feed,
        })
        try:
            urlfetch.fetch("http://blogsearch.google.com/ping?" + args)
        except:
            pass
        args = urllib.urlencode({
            "url": feed,
            "supid": self.generate_sup_id(feed),
        })
        try:
            urlfetch.fetch("http://friendfeed.com/api/public-sup-ping?" + args)
        except:
            pass
        args = urllib.urlencode({
            "bloglink": "http://" + self.request.host + "/",
        })
        try:
            urlfetch.fetch("http://www.feedburner.com/fb/a/pingSubmit?" + args)
        except:
            pass
        args = urllib.urlencode({
            "hub.mode": "publish",
            "hub.url": feed,
        })
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            result = urlfetch.fetch("http://pubsubhubbub.appspot.com/",
                payload=args, method=urlfetch.POST, headers=headers)
        except:
            pass

    def get_error_html(self, status_code, **kwargs):
        if status_code == 404:
            self.write(self.render_string("404.html"))
        else:
            return tornado.web.RequestHandler.get_error_html(self, status_code,
                                                             **kwargs)

    def head(self, *args):
        if self.get_argument("format", None) == "atom":
            self.set_sup_header()


class HomeHandler(BaseHandler):
    def get(self):
        cursor = self.get_argument("cursor", None)
        limit = self.application.settings.get("num_home", 5)
        # The cursor is in the cache key. We never evict caches other than
        # 'home_entries:None:5' which is the front page. Doing so will cause
        # the cursors to change and thus change the key. Eventually memcache
        # will evict the cache keys containing cursors that are never fetched
        # once memory usage goes high because stale cursors should never get hit
        # and hopefully the App Engine memcache service prunes cold keys first
        cache_key = 'home_entries:%s:%s' % (cursor, limit)
        cached_data = memcache.get(cache_key)
        if cached_data:
            (entries, new_cursor) = cached_data
        else:
            q = db.Query(Entry).filter("hidden =", False).order("-published")
            if cursor:
                try:
                    q.with_cursor(cursor)
                except (db.BadRequestError, db.BadValueError):
                    cursor = None
            entries = q.fetch(limit=limit)
            new_cursor = q.cursor() if len(entries) == limit else None
            memcache.add(cache_key, (entries, new_cursor))
        self.render("home.html", entries=entries, cursor=new_cursor)


class AboutHandler(BaseHandler):
    @tornado.web.removeslash
    def get(self):
        self.render("about.html")


class ArchiveHandler(BaseHandler):
    @tornado.web.removeslash
    def get(self):
        q = db.Query(Entry).filter("hidden =", False).order("-published")
        cursor = self.get_argument("cursor", None)
        if cursor:
            try:
                q.with_cursor(cursor)
            except (db.BadRequestError, db.BadValueError):
                cursor = None
        limit = self.application.settings.get("num_archive", 10)
        entries = q.fetch(limit=limit)
        cursor = q.cursor() if len(entries) == limit else None
        self.render("archive.html", entries=entries, cursor=cursor)


class ComposeHandler(BaseHandler):
    @administrator
    def get(self):
        key = self.get_argument("key", None)
        try:
            entry = Entry.get(key) if key else None
        except db.BadKeyError:
            entry = None
        self.render("compose.html", entry=entry)

    @administrator
    def post(self):
        key = self.get_argument("key", None)
        if key:
            try:
                entry = Entry.get(key)
            except db.BadKeyError:
                self.redirect("/")
                return
            entry.body = self.get_argument("body")
            entry.title = self.get_argument("title")
        else:
            title = self.get_argument("title")
            slug = self.slugify(title)
            if not slug:
                slug = "entry"
            original_slug = slug
            while db.Query(Entry).filter("slug = ", slug).get():
                slug = original_slug + "-" + uuid.uuid4().hex[:2]
            entry = Entry(
                author=self.current_user,
                body=self.get_argument("body"),
                slug=slug,
                title=title,
            )
        tags = set([self.slugify(unicode(tag)) for tag in
            self.get_argument("tags", "").split(",")])
        tags = [db.Category(tag) for tag in tags if tag]
        entry.tags = tags
        entry.hidden = bool(self.get_argument("hidden", False))
        entry.put()
        memcache.delete('home_entries:%s:%s' %
          (None, self.application.settings.get("num_home", 5))
        )
        if not key and not entry.hidden:
            self.ping()
        self.redirect("/" + entry.slug)


class DeleteHandler(BaseHandler):
    @administrator
    def get(self):
        key = self.get_argument("key")
        try:
            entry = Entry.get(key)
        except db.BadKeyError:
            raise tornado.web.HTTPError(404)
        self.render("delete.html", entry=entry)

    @administrator
    def post(self):
        key = self.get_argument("key")
        try:
            entry = Entry.get(key)
        except db.BadKeyError:
            raise tornado.web.HTTPError(404)
        entry.delete()
        self.redirect("/")


class HideHandler(BaseHandler):
    @administrator
    def get(self):
        key = self.get_argument("key")
        try:
            entry = Entry.get(key)
        except db.BadKeyError:
            raise tornado.web.HTTPError(404)
        self.render("hide.html", entry=entry)

    @administrator
    def post(self):
        key = self.get_argument("key")
        try:
            entry = Entry.get(key)
        except db.BadKeyError:
            raise tornado.web.HTTPError(404)
        entry.hidden = not bool(self.get_argument("unhide", False))
        entry.put()
        self.redirect("/")


class OldEntryHandler(BaseHandler):
    @tornado.web.removeslash
    def get(self, slug):
        self.redirect("/" + slug)

    @tornado.web.removeslash
    def head(self, slug):
        return self.get(slug)
        

class TagHandler(BaseHandler):
    @tornado.web.removeslash
    def get(self, tag):
        q = db.Query(Entry).filter("hidden =", False).filter("tags =", tag)
        q.order("-published")
        self.render("tag.html", entries=q, tag=tag)


class CatchAllHandler(BaseHandler):
    def __init__(self, *args, **kwargs):
        BaseHandler.__init__(self, *args, **kwargs)
        self.entry = None
        slug = self.request.path[1:]
        if slug:
            self.entry = db.Query(Entry).filter("slug =", slug).get()

    @tornado.web.removeslash
    def get(self):
        if self.entry:
            return self.render("entry.html", entry=self.entry,
                               entries=[self.entry])
        self.set_status(404)
        self.render("404.html")

    def head(self):
        if not self.entry:
            self.set_status(404)


class EntryModule(tornado.web.UIModule):
    def render(self, entry, show_comments=False):
        self.show_comments = show_comments
        return self.render_string("modules/entry.html", entry=entry,
            show_comments=show_comments)

class MediaRSSModule(tornado.web.UIModule):
    def render(self, entry):
        soup = BeautifulSoup.BeautifulSoup(entry.body,
            parseOnlyThese=BeautifulSoup.SoupStrainer("img"))
        imgs = soup.findAll("img")
        thumbnails = []
        for img in imgs:
            if "nomediarss" in img.get("class", "").split():
                continue
            thumbnails.append({
                "url": img["src"],
                "title": img.get("title", img.get("alt", "")),
                "width": img.get("width", ""),
                "height": img.get("height", ""),
            })
        return self.render_string("modules/mediarss.html", entry=entry,
            thumbnails=thumbnails) 


class EntrySmallModule(tornado.web.UIModule):
    def render(self, entry, show_date=False):
        return self.render_string("modules/entry-small.html", entry=entry,
            show_date=show_date)


class RecentEntriesModule(tornado.web.UIModule):
    def render(self):
        limit = self.handler.application.settings.get("num_home", 5)
        cache_key = 'home_entries:%s:%s' % (None, limit)
        cached_data = memcache.get(cache_key)
        if cached_data:
            (entries, new_cursor) = cached_data
        else:
            q = db.Query(Entry).filter("hidden =", False).order("-published")
            entries = q.fetch(limit=limit)
        return self.render_string("modules/recententries.html", entries=entries)


class NavigationModule(tornado.web.UIModule):
    def render(self, cursor):
        kwargs = {
            "cursor": cursor,
        }
        previous = self.request.path + "?" + urllib.urlencode(kwargs)
        return self.render_string("modules/navigation.html", previous=previous)


settings = {
    "autoescape": None,
    "blog_author": "Benjamin Golub",
    "blog_title": "Benjamin Golub",
    "fb_admins": "15500414",
    "fb_app_id": "143871635676545",
    "debug": os.environ.get("SERVER_SOFTWARE", "").startswith("Development/"),
    "template_path": os.path.join(os.path.dirname(__file__), "templates"),
    "ui_modules": {
        "Entry": EntryModule,
        "EntrySmall": EntrySmallModule,
        "MediaRSS": MediaRSSModule,
        "RecentEntries": RecentEntriesModule,
        "Navigation": NavigationModule,
    },
    "xsrf_cookies": True,
}

application = tornado.wsgi.WSGIApplication([
    (r"/", HomeHandler),
    (r"/about/?", AboutHandler),
    (r"/archive/?", ArchiveHandler),
    (r"/compose", ComposeHandler),
    (r"/delete", DeleteHandler),
    (r"/e/([\w-]+)/?", OldEntryHandler),
    (r"/feed/?", tornado.web.RedirectHandler, {"url": "/?format=atom"}),
    (r"/hide", HideHandler),
    (r"/t/([\w-]+)/?", TagHandler),
    (r".*", CatchAllHandler),
], **settings)

def main():
    wsgiref.handlers.CGIHandler().run(application)

if __name__ == "__main__":
    main() 
