import functools
import hashlib
import os
import re
import tornado.web
import tornado.wsgi
import unicodedata
import urllib
import uuid
import wsgiref.handlers

from django.utils import feedgenerator

from google.appengine.ext import db
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
            self.set_sup_header()
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

    def set_sup_header(self, url=None):
        sup_id = self.generate_sup_id(url)
        self.set_header("X-SUP-ID",
            "http://friendfeed.com/api/public-sup.json#" + sup_id) 

    def ping(self):
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

    def get_error_html(self, status_code):
        if status_code == 404:
            self.write(self.render_string("404.html"))
        else:
            return tornado.web.RequestHandler.get_error_html(self, status_code)

    def head(self, *args):
        if self.get_argument("format", None) == "atom":
            self.set_sup_header()


class HomeHandler(BaseHandler):
    def get(self):
        entries = db.Query(Entry).order("-published").fetch(limit=5)
        self.recent_entries = entries
        self.render("home.html", entries=entries)


class AboutHandler(BaseHandler):
    @tornado.web.removeslash
    def get(self):
        self.render("about.html")


class ArchiveHandler(BaseHandler):
    @tornado.web.removeslash
    def get(self):
        entries = db.Query(Entry).order("-published")
        self.recent_entries = entries[:5]
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
        entry.tags = tags
        entry.put()
        if not key:
            self.ping()
        self.redirect("/e/" + entry.slug)


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


class EntryHandler(BaseHandler):
    @tornado.web.removeslash
    def get(self, slug):
        entry = db.Query(Entry).filter("slug =", slug).get()
        if not entry:
            raise tornado.web.HTTPError(404)
        self.render("entry.html", entry=entry)

    def head(self, slug):
        entry = db.Query(Entry).filter("slug =", slug).get()
        if not entry:
            raise tornado.web.HTTPError(404)


class TagHandler(BaseHandler):
    @tornado.web.removeslash
    def get(self, tag):
        entries = db.Query(Entry).filter("tags =", tag).order("-published")
        self.render("tag.html", entries=entries, tag=tag)


class CatchAllHandler(BaseHandler):
    @tornado.web.removeslash
    def get(self):
        self.set_status(404)
        self.render("404.html")

    def head(self):
        self.set_status(404)


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


class EntrySmallModule(tornado.web.UIModule):
    def render(self, entry, show_date=False):
        return self.render_string("modules/entry-small.html", entry=entry,
            show_date=show_date)


class RecentEntriesModule(tornado.web.UIModule):
    def render(self):
        entries = getattr(self.handler, "recent_entries", 
            db.Query(Entry).order("-published").fetch(limit=5))
        return self.render_string("modules/recententries.html", entries=entries)


settings = {
    "blog_author": "Benjamin Golub",
    "blog_title": "Benjamin Golub",
    "debug": os.environ.get("SERVER_SOFTWARE", "").startswith("Development/"),
    "template_path": os.path.join(os.path.dirname(__file__), "templates"),
    "ui_modules": {
        "Entry": EntryModule,
        "EntrySmall": EntrySmallModule,
        "RecentEntries": RecentEntriesModule,
    },
    "xsrf_cookies": True,
}

application = tornado.wsgi.WSGIApplication([
    (r"/", HomeHandler),
    (r"/about/?", AboutHandler),
    (r"/archive/?", ArchiveHandler),
    (r"/compose", ComposeHandler),
    (r"/delete", DeleteHandler),
    (r"/e/([\w-]+)/?", EntryHandler),
    (r"/feed/?", tornado.web.RedirectHandler, {"url": "/?format=atom"}),
    (r"/t/([\w-]+)/?", TagHandler),
    (r".*", CatchAllHandler),
], **settings)

def main():
    wsgiref.handlers.CGIHandler().run(application)

if __name__ == "__main__":
    main() 
