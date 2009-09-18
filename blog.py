import functools
import hashlib
import os.path
import re
import tornado.web
import tornado.wsgi
import unicodedata
import urllib
import uuid
import wsgiref.handlers

from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.ext import db

from django.utils import feedgenerator


class Entry(db.Model):
    author = db.UserProperty()
    title = db.StringProperty(required=True)
    slug = db.StringProperty(required=True)
    body = db.TextProperty(required=True)
    published = db.DateTimeProperty(auto_now_add=True)
    updated = db.DateTimeProperty(auto_now=True)
    tags = db.ListProperty(db.Category)

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
        if "entries" in kwargs and format == "atom":
            feed = feedgenerator.Atom1Feed(
                title=self.application.settings["blog_title"],
                description=self.application.settings["blog_title"],
                link=self.request.path,
                language="en",
            )
            for entry in kwargs["entries"]:
                feed.add_item(
                    title=entry.title,
                    link="http://" + self.request.host + "/e/" + entry.slug,
                    description=entry.body,
                    author_name=entry.author.nickname(),
                    pubdate=entry.published,
                    categories=entry.tags,
                )
            data = feed.writeString("utf-8")
            self.set_header("Content-Type", "application/atom+xml")
            sup_id = self.generate_sup_id()
            self.set_header("X-SUP-ID",
                "http://friendfeed.com/api/public-sup.json#" + sup_id) 
            self.write(data)
            return
        return tornado.web.RequestHandler.render(self, template_name, **kwargs)

    def slugify(self, value):
        slug = unicodedata.normalize("NFKD", value).encode(
            "ascii", "ignore")
        slug = re.sub(r"[^\w]+", " ", slug)
        return "-".join(slug.lower().strip().split())

    def generate_sup_id(self, url=None):
        return hashlib.md5(url or self.request.full_url()).hexdigest()[:10]

    def ping(self):
        feed = "http://" + self.request.host + "/?format=atom"
        args = urllib.urlencode({
            "name": self.application.settings["blog_title"],
            "url": "http://" + self.request.host + "/",
            "changesURL": feed,
        })
        urlfetch.fetch("http://blogsearch.google.com/ping?" + args)
        args = urllib.urlencode({
            "url": feed,
            "supid": self.generate_sup_id(feed),
        })
        urlfetch.fetch("http://friendfeed.com/api/public-sup-ping?" + args)
        args = urllib.urlencode({
            "bloglink": "http://" + self.request.host + "/",
        })
        urlfetch.fetch("http://www.feedburner.com/fb/a/pingSubmit?" + args)

    def get_error_html(self, status_code):
        if status_code == 404:
            self.write(self.render_string("404.html"))
        else:
            return tornado.web.RequestHandler.get_error_html(self, status_code)


class HomeHandler(BaseHandler):
    def get(self):
        start = self.get_integer_argument("start", 0)
        entries = db.Query(Entry).order("-published").fetch(limit=5,
            offset=start)
        if not entries and start > 0:
            self.redirect("/")
            return
        next = max(start - 5, 0)
        previous = start + 5 if len(entries) == 5 else None
        self.render("home.html", entries=entries, start=start, next=next,
            previous=previous) 


class ArchiveHandler(BaseHandler):
    def get(self):
        entries = db.Query(Entry).order("-published")
        self.render("archive.html", entries=entries)


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
        if tags:
            entry.tags = tags
        entry.put()
        if not key:
            self.ping()
        self.redirect("/e/" + entry.slug)


class EntryHandler(BaseHandler):
    def get(self, slug):
        entry = db.Query(Entry).filter("slug =", slug).get()
        if not entry:
            raise tornado.web.HTTPError(404)
        self.render("entry.html", entry=entry)


class TagHandler(BaseHandler):
    def get(self, tag):
        entries = db.Query(Entry).filter("tags =", tag).order("-published")
        self.render("tag.html", entries=entries, tag=tag)

class CatchAllHandler(BaseHandler):
    def get(self):
        self.render("404.html")


class EntryModule(tornado.web.UIModule):
    def render(self, entry, show_comments=False):
        self.show_comments = show_comments
        self.show_count = not show_comments
        return self.render_string("modules/entry.html", entry=entry,
            show_comments=show_comments)

    def embedded_javascript(self):
        if self.show_count:
            return self.render_string("disquscount.js")
        return None

    def javascript_files(self):
        if self.show_comments:
            return ["http://disqus.com/forums/benjamingolub/embed.js"]
        return None


class RecentEntriesModule(tornado.web.UIModule):
    def render(self):
        entries = db.Query(Entry).order("-published").fetch(limit=5)
        return self.render_string("modules/recententries.html", entries=entries)


settings = {
    "blog_title": "Benjamin Golub's Blog",
    "debug": os.environ.get("SERVER_SOFTWARE", "").startswith("Development/"),
    "template_path": os.path.join(os.path.dirname(__file__), "templates"),
    "ui_modules": {
        "Entry": EntryModule,
        "RecentEntries": RecentEntriesModule,
    },
    "xsrf_cookies": True,
}

application = tornado.wsgi.WSGIApplication([
    (r"/", HomeHandler),
    (r"/archive", ArchiveHandler),
    (r"/compose", ComposeHandler),
    (r"/e/([\w-]+)", EntryHandler),
    (r"/t/([\w-]+)", TagHandler),
    (r".*", CatchAllHandler),
], **settings)

def main():
    wsgiref.handlers.CGIHandler().run(application)

if __name__ == "__main__":
    main() 
