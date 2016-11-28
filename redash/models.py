import datetime
import functools
import hashlib
import itertools
import json
import logging
import os
import threading
import time

from funcy import project

from flask_sqlalchemy import SQLAlchemy
from flask.ext.sqlalchemy import SignallingSession
from flask_login import UserMixin, AnonymousUserMixin
from sqlalchemy.dialects import postgresql
from sqlalchemy.event import listens_for
from sqlalchemy.types import TypeDecorator

from passlib.apps import custom_app_context as pwd_context
from playhouse.gfk import GFKField, BaseModel
from playhouse.postgres_ext import ArrayField, DateTimeTZField



from redash import redis_connection, settings, utils
from redash.destinations import get_destination, get_configuration_schema_for_destination_type
from redash.metrics.database import MeteredPostgresqlExtDatabase, MeteredModel
from redash.permissions import has_access, view_only
from redash.query_runner import get_query_runner, get_configuration_schema_for_query_runner_type
from redash.utils import generate_token, json_dumps
from redash.utils.configuration import ConfigurationContainer

db = SQLAlchemy()
Column = functools.partial(db.Column, nullable=False)

# AccessPermission and Change use a 'generic foreign key' approach to refer to
# either queries or dashboards.
# TODO replace this with association tables.
_gfk_types = {}

class GFKBase(object):
    """
    Compatibility with 'generic foreign key' approach Peewee used.
    """
    # XXX Replace this with table-per-association.
    object_type = Column(db.String(255))
    object_id = Column(db.Integer)

    _object = None

    @property
    def object(self):
        session = object_session(self)
        if self._object or not session:
            return self._object
        else:
            object_class = _gfk_types[self.object_type]
            self._object = session.query(object_class).filter(
                object_class.id == self.object_id).first()
            return self._object

    @object.setter
    def object(self, value):
        self._object = value
        self.object_type = value.__class__.__tablename__
        self.object_id = value.id


# # Support for cast operation on database fields
# @peewee.Node.extend()
# def cast(self, as_type):
#     return peewee.Expression(self, '::', peewee.SQL(as_type))


class PseudoJSON(TypeDecorator):
    impl = db.Text
    def process_bind_param(self, value, dialect):
        return json_dumps(value)
    def process_result_value(self, value, dialect):
        if not value:
            return value
        return json.loads(value)


class TimestampMixin(object):
    updated_at = Column(db.DateTime(True), default=db.func.now(),
                           onupdate=db.func.now(), nullable=False)
    created_at = Column(db.DateTime(True), default=db.func.now(),
                           nullable=False)


class ChangeTrackingMixin(object):
    skipped_fields = ('id', 'created_at', 'updated_at', 'version')
    _clean_values = None

    def prep_cleanvalues(self):
        self.__dict__['_clean_values'] = {}
        for c in self.__class__.__table__.c:
            self._clean_values[c.name] = None

    def __setattr__(self, key, value):
        if self._clean_values is None:
            self.prep_cleanvalues()
        if key in self._clean_values:
            previous = getattr(self, key)
            self._clean_values[key] = previous

        super(ChangeTrackingMixin, self).__setattr__(key, value)

    def record_changes(self, changed_by):
        changes = {}
        for k, v in self._clean_values.iteritems():
            if k not in self.skipped_fields:
                changes[k] = {'previous': v, 'current': getattr(self, k)}
        db.session.flush()
        db.session.add(Change(object_type=self.__class__.__tablename__,
                           object=self,
                           object_version=self.version,
                           user=changed_by,
                           change=changes))



class ConflictDetectedError(Exception):
    pass

class BelongsToOrgMixin(object):
    @classmethod
    def get_by_id_and_org(cls, object_id, org):
        return cls.query.filter(cls.id == object_id, cls.org == org).one_or_none()


class PermissionsCheckMixin(object):
    def has_permission(self, permission):
        return self.has_permissions((permission,))

    def has_permissions(self, permissions):
        has_permissions = reduce(lambda a, b: a and b,
                                 map(lambda permission: permission in self.permissions,
                                     permissions),
                                 True)

        return has_permissions

class AnonymousUser(AnonymousUserMixin, PermissionsCheckMixin):
    @property
    def permissions(self):
        return []


class ApiUser(UserMixin, PermissionsCheckMixin):
    def __init__(self, api_key, org, groups, name=None):
        self.object = None
        if isinstance(api_key, basestring):
            self.id = api_key
            self.name = name
        else:
            self.id = api_key.api_key
            self.name = "ApiKey: {}".format(api_key.id)
            self.object = api_key.object
        self.groups = groups
        self.org = org

    def __repr__(self):
        return u"<{}>".format(self.name)

    @property
    def permissions(self):
        return ['view_query']

    def has_access(self, obj, access_type):
        return False


class Organization(TimestampMixin, db.Model):
    SETTING_GOOGLE_APPS_DOMAINS = 'google_apps_domains'
    SETTING_IS_PUBLIC = "is_public"

    id = Column(db.Integer, primary_key=True)
    name = Column(db.String(255))
    slug = Column(db.String(255), unique=True)
    settings = Column(PseudoJSON)
    groups = db.relationship("Group", lazy="dynamic")

    __tablename__ = 'organizations'

    def __repr__(self):
        return u"<Organization: {}, {}>".format(self.id, self.name)

    @classmethod
    def get_by_slug(cls, slug):
        return cls.query.filter(cls.slug == slug).first()

    @property
    def default_group(self):
        return self.groups.filter(Group.name == 'default', Group.type == Group.BUILTIN_GROUP).first()

    @property
    def google_apps_domains(self):
        return self.settings.get(self.SETTING_GOOGLE_APPS_DOMAINS, [])

    @property
    def is_public(self):
        return self.settings.get(self.SETTING_IS_PUBLIC, False)

    @property
    def admin_group(self):
        return self.groups.filter(Group.name == 'admin', Group.type == Group.BUILTIN_GROUP).first()

    def has_user(self, email):
        return self.users.filter(User.email == email).count() == 1


class Group(db.Model, BelongsToOrgMixin):
    DEFAULT_PERMISSIONS = ['create_dashboard', 'create_query', 'edit_dashboard', 'edit_query',
                           'view_query', 'view_source', 'execute_query', 'list_users', 'schedule_query',
                           'list_dashboards', 'list_alerts', 'list_data_sources']

    BUILTIN_GROUP = 'builtin'
    REGULAR_GROUP = 'regular'

    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey('organizations.id'))
    org = db.relationship(Organization, back_populates="groups")
    type = Column(db.String(255), default=REGULAR_GROUP)
    name = Column(db.String(100))
    permissions = Column(postgresql.ARRAY(db.String(255)),
                         default=DEFAULT_PERMISSIONS)
    created_at = Column(db.DateTime(True), default=db.func.now())

    __tablename__ = 'groups'

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'permissions': self.permissions,
            'type': self.type,
            'created_at': self.created_at
        }

    @classmethod
    def all(cls, org):
        return cls.query.filter(cls.org == org)

    @classmethod
    def members(cls, group_id):
        return User.query.filter(group_id == db.func.any_(User.c.groups))

    @classmethod
    def find_by_name(cls, org, group_names):
        result = cls.query.filter(cls.org == org, cls.name.in_(group_names))
        return list(result)

    def __unicode__(self):
        return unicode(self.id)

def create_group_hack(*a, **kw):
    g = Group(*a, **kw)
    db.session.add(g)
    db.commit()
    return g.id

class User(TimestampMixin, db.Model, BelongsToOrgMixin, UserMixin, PermissionsCheckMixin):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey('organizations.id'))
    org = db.relationship(Organization, backref="users")
    name = Column(db.String(320))
    email = Column(db.String(320))
    password_hash = Column(db.String(128), nullable=True)
    #XXX replace with association table
    group_ids = Column('groups', postgresql.ARRAY(db.Integer), nullable=True)
    api_key = Column(db.String(40),
                     default=lambda: generate_token(40),
                     unique=True)

    __tablename__ = 'users'
    __table_args__ = (db.Index('users_org_id_email', 'org_id', 'email', unique=True),)

    def __init__(self, *args, **kwargs):
        super(User, self).__init__(*args, **kwargs)

    def to_dict(self, with_api_key=False):
        d = {
            'id': self.id,
            'name': self.name,
            'email': self.email,
            'gravatar_url': self.gravatar_url,
            'groups': self.groups,
            'updated_at': self.updated_at,
            'created_at': self.created_at
        }

        if self.password_hash is None:
            d['auth_type'] = 'external'
        else:
            d['auth_type'] = 'password'

        if with_api_key:
            d['api_key'] = self.api_key

        return d

    @property
    def gravatar_url(self):
        email_md5 = hashlib.md5(self.email.lower()).hexdigest()
        return "https://www.gravatar.com/avatar/%s?s=40" % email_md5

    @property
    def permissions(self):
        # TODO: this should be cached.
        return list(itertools.chain(*[g.permissions for g in
                                      Group.select().where(Group.id << self.groups)]))

    @classmethod
    def get_by_email_and_org(cls, email, org):
        return cls.get(cls.email == email, cls.org == org)

    @classmethod
    def get_by_api_key_and_org(cls, api_key, org):
        return cls.get(cls.api_key == api_key, cls.org == org)

    @classmethod
    def all(cls, org):
        return cls.select().where(cls.org == org)

    @classmethod
    def find_by_email(cls, email):
        return cls.select().where(cls.email == email)

    def __unicode__(self):
        return u'%s (%s)' % (self.name, self.email)

    def hash_password(self, password):
        self.password_hash = pwd_context.encrypt(password)

    def verify_password(self, password):
        return self.password_hash and pwd_context.verify(password, self.password_hash)

    def update_group_assignments(self, group_names):
        groups = Group.find_by_name(self.org, group_names)
        groups.append(self.org.default_group)
        self.group_ids = [g.id for g in groups]
        db.session.add(self)

    def has_access(self, obj, access_type):
        return AccessPermission.exists(obj, access_type, grantee=self)


class Configuration(TypeDecorator):

    impl = db.Text

    def process_bind_param(self, value, dialect):
        return value.to_json()

    def process_result_value(self, value, dialect):
        return ConfigurationContainer.from_json(value)


class DataSource(BelongsToOrgMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey('organizations.id'))
    org = db.relationship(Organization, backref="data_sources")

    name = Column(db.String(255))
    type = Column(db.String(255))
    options = Column(Configuration)
    queue_name = Column(db.String(255), default="queries")
    scheduled_queue_name = Column(db.String(255), default="scheduled_queries")
    created_at = Column(db.DateTime(True), default=db.func.now())

    data_source_groups = db.relationship("DataSourceGroup", back_populates="data_source")
    __tablename__ = 'data_sources'
    __table_args__ = (db.Index('data_sources_org_id_name', 'org_id', 'name'),)

    def to_dict(self, all=False, with_permissions_for=None):
        d = {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'syntax': self.query_runner.syntax,
            'paused': self.paused,
            'pause_reason': self.pause_reason
        }

        if all:
            schema = get_configuration_schema_for_query_runner_type(self.type)
            self.options.set_schema(schema)
            d['options'] = self.options.to_dict(mask_secrets=True)
            d['queue_name'] = self.queue_name
            d['scheduled_queue_name'] = self.scheduled_queue_name
            d['groups'] = self.groups

        if with_permissions_for is not None:
            d['view_only'] = db.session.query(DataSourceGroup.view_only).filter(
                DataSourceGroup.group == with_permissions_for,
                DataSourceGroup.data_source == self).get()

        return d

    def __unicode__(self):
        return self.name

    @classmethod
    def create_with_group(cls, *args, **kwargs):
        data_source = cls.create(*args, **kwargs)
        DataSourceGroup.create(data_source=data_source, group=data_source.org.default_group)
        return data_source

    def get_schema(self, refresh=False):
        key = "data_source:schema:{}".format(self.id)

        cache = None
        if not refresh:
            cache = redis_connection.get(key)

        if cache is None:
            query_runner = self.query_runner
            schema = sorted(query_runner.get_schema(get_stats=refresh), key=lambda t: t['name'])

            redis_connection.set(key, json.dumps(schema))
        else:
            schema = json.loads(cache)

        return schema

    def _pause_key(self):
        return 'ds:{}:pause'.format(self.id)

    @property
    def paused(self):
        return redis_connection.exists(self._pause_key())

    @property
    def pause_reason(self):
        return redis_connection.get(self._pause_key())

    def pause(self, reason=None):
        redis_connection.set(self._pause_key(), reason)

    def resume(self):
        redis_connection.delete(self._pause_key())

    def add_group(self, group, view_only=False):
        dsg = DataSourceGroup(group=group, data_source=self, view_only=view_only)
        db.session.add(dsg)

    def remove_group(self, group):
        db.session.query(DataSourceGroup).filter(
            DataSourceGroup.group == group,
            DataSourceGroup.data_source == self).delete()

    def update_group_permission(self, group, view_only):
        dsg = db.session.query(DataSourceGroup).filter(
            DataSourceGroup.group == group,
            DataSourceGroup.data_source == self)
        dsg.view_only = view_only
        db.session.add(dsg)

    @property
    def query_runner(self):
        return get_query_runner(self.type, self.options)

    @classmethod
    def all(cls, org, groups=None):
        data_sources = cls.select().where(cls.org==org).order_by(cls.id.asc())

        if groups:
            data_sources = data_sources.join(DataSourceGroup).where(DataSourceGroup.group << groups)

        return data_sources

    #XXX examine call sites to see if a regular SQLA collection would work better
    @property
    def groups(self):
        groups = db.session.query(DataSourceGroup).filter(
            DataSourceGroup.data_source == self)
        return dict(map(lambda g: (g.group_id, g.view_only), groups))


class DataSourceGroup(db.Model):
    #XXX drop id, use datasource/group as PK
    id = Column(db.Integer, primary_key=True)
    data_source_id = Column(db.Integer, db.ForeignKey("data_sources.id"))
    data_source = db.relationship(DataSource, back_populates="data_source_groups")
    group_id = Column(db.Integer, db.ForeignKey("groups.id"))
    group = db.relationship(Group, backref="data_sources")
    view_only = Column(db.Boolean, default=False)

    __tablename__ = "data_source_groups"


class QueryResult(db.Model, BelongsToOrgMixin):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey('organizations.id'))
    org = db.relationship(Organization)
    data_source_id = Column(db.Integer, db.ForeignKey("data_sources.id"))
    data_source = db.relationship(DataSource)
    query_hash = Column(db.String(32), index=True)
    query = Column(db.Text)
    data = Column(db.Text)
    runtime = Column(postgresql.DOUBLE_PRECISION)
    retrieved_at = Column(db.DateTime(True))

    __tablename__ = 'query_results'

    def to_dict(self):
        return {
            'id': self.id,
            'query_hash': self.query_hash,
            'query': self.query,
            'data': json.loads(self.data),
            'data_source_id': self.data_source_id,
            'runtime': self.runtime,
            'retrieved_at': self.retrieved_at
        }

    @classmethod
    def unused(cls, days=7):
        age_threshold = datetime.datetime.now() - datetime.timedelta(days=days)

        unused_results = (db.session.query(QueryResult).filter(
            Query.id == None, QueryResult.retrieved_at < age_threshold)
            .outerjoin(Query))

        return unused_results

    @classmethod
    def get_latest(cls, data_source, query, max_age=0):
        query_hash = utils.gen_query_hash(query)

        if max_age == -1:
            q = db.session.query(QueryResult).filter(
                cls.query_hash == query_hash,
                cls.data_source == data_source).order_by(
                    QueryResult.retrieved_at.desc())
        else:
            q = db.session.query(QueryResult).filter(
                QueryResult.query_hash == query_hash,
                QueryResult.data_source == data_source,
                db.func.timezone('utc', QueryResult.retrieved_at) +
                datetime.timedelta(seconds=max_age) >=
                db.func.timezone('utc', db.func.now())
                ).order_by(QueryResult.retrieved_at.desc())

        return q.first()

    @classmethod
    def store_result(cls, org, data_source, query_hash, query, data, run_time, retrieved_at):
        query_result = cls(org=org,
                           query_hash=query_hash,
                           query=query,
                           runtime=run_time,
                           data_source=data_source,
                           retrieved_at=retrieved_at,
                           data=data)
        db.session.add(query_result)
        logging.info("Inserted query (%s) data; id=%s", query_hash, query_result.id)

        # TODO: Investigate how big an impact this select-before-update makes.
        queries = db.session.query(Query).filter(
            Query.query_hash == query_hash,
            Query.data_source == data_source)
        for q in queries:
            q.latest_query_data = query_result
            db.session.add(q)
        query_ids = [q.id for q in queries] 
        logging.info("Updated %s queries with result (%s).", len(query_ids), query_hash)

        return query_result, query_ids

    def __unicode__(self):
        return u"%d | %s | %s" % (self.id, self.query_hash, self.retrieved_at)

    @property
    def groups(self):
        return self.data_source.groups


def should_schedule_next(previous_iteration, now, schedule):
    if schedule.isdigit():
        ttl = int(schedule)
        next_iteration = previous_iteration + datetime.timedelta(seconds=ttl)
    else:
        hour, minute = schedule.split(':')
        hour, minute = int(hour), int(minute)

        # The following logic is needed for cases like the following:
        # - The query scheduled to run at 23:59.
        # - The scheduler wakes up at 00:01.
        # - Using naive implementation of comparing timestamps, it will skip the execution.
        normalized_previous_iteration = previous_iteration.replace(hour=hour, minute=minute)
        if normalized_previous_iteration > previous_iteration:
            previous_iteration = normalized_previous_iteration - datetime.timedelta(days=1)

        next_iteration = (previous_iteration + datetime.timedelta(days=1)).replace(hour=hour, minute=minute)

    return now > next_iteration


def generate_query_api_key(ctx):
    return hashlib.sha1(u''.join((
        str(time.time()), ctx.current_parameters['query'],
        str(ctx.current_parameters['user_id']),
        ctx.current_parameters['name'])).encode('utf-8')).hexdigest()


class Query(ChangeTrackingMixin, TimestampMixin, BelongsToOrgMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    version = Column(db.Integer)
    org_id = Column(db.Integer, db.ForeignKey('organizations.id'))
    org = db.relationship(Organization, backref="queries")
    data_source_id = Column(db.Integer, db.ForeignKey("data_sources.id"), nullable=True)
    data_source = db.relationship(DataSource)
    latest_query_data_id = Column(db.Integer, db.ForeignKey("query_results.id"), nullable=True)
    latest_query_data = db.relationship(QueryResult)
    name = Column(db.String(255))
    description = Column(db.String(4096), nullable=True)
    query = Column(db.Text)
    query_hash = Column(db.String(32))
    api_key = Column(db.String(40), default=generate_query_api_key)
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User, foreign_keys=[user_id])
    last_modified_by_id = Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    last_modified_by = db.relationship(User, backref="modified_queries",
                                       foreign_keys=[last_modified_by_id])
    is_archived = Column(db.Boolean, default=False, index=True)
    is_draft = Column(db.Boolean, default=False, index=True)
    schedule = Column(db.String(10), nullable=True)
    options = Column(PseudoJSON, default={})

    __tablename__ = 'queries'
    __mapper_args__ = {
        "version_id_col": version
        }

    def to_dict(self, with_stats=False, with_visualizations=False, with_user=True, with_last_modified_by=True):
        d = {
            'id': self.id,
            'latest_query_data_id': self._data.get('latest_query_data', None),
            'name': self.name,
            'description': self.description,
            'query': self.query,
            'query_hash': self.query_hash,
            'schedule': self.schedule,
            'api_key': self.api_key,
            'is_archived': self.is_archived,
            'is_draft': self.is_draft,
            'updated_at': self.updated_at,
            'created_at': self.created_at,
            'data_source_id': self.data_source_id,
            'options': self.options,
            'version': self.version
        }

        if with_user:
            d['user'] = self.user.to_dict()
        else:
            d['user_id'] = self.user_id

        if with_last_modified_by:
            d['last_modified_by'] = self.last_modified_by.to_dict() if self.last_modified_by is not None else None
        else:
            d['last_modified_by_id'] = self.last_modified_by_id

        if with_stats:
            d['retrieved_at'] = self.retrieved_at
            d['runtime'] = self.runtime

        if with_visualizations:
            d['visualizations'] = [vis.to_dict(with_query=False)
                                   for vis in self.visualizations]

        return d

    def archive(self, user=None):
        db.session.add(self)
        self.is_archived = True
        self.schedule = None

        for vis in self.visualizations:
            for w in vis.widgets:
                db.session.delete(w)

        for a in self.alerts:
            db.session.delete(a)

        if user:
            self.record_changes(user)

    @classmethod
    def all_queries(cls, groups, drafts=False):
        q = (db.session.query(Query)
            .outerjoin(QueryResult)
            .join(User, Query.user_id == User.id)
            .join(DataSourceGroup, Query.data_source_id == DataSourceGroup.data_source_id)
            .filter(Query.is_archived == False)
            .filter(DataSourceGroup.group_id.in_([g.id for g in groups]))\
            .group_by(Query.id, User.id, QueryResult.id, QueryResult.retrieved_at, QueryResult.runtime)
            .order_by(Query.created_at.desc()))

        if drafts:
            q = q.filter(Query.is_draft == True)
        else:
            q = q.filter(Query.is_draft == False)

        return q

    @classmethod
    def by_user(cls, user, drafts):
        return cls.all_queries(user.groups, drafts).filter(Query.user == user)

    @classmethod
    def outdated_queries(cls):
        queries = (db.session.query(Query)
                   .join(QueryResult)
                   .join(DataSource)
                   .filter(Query.schedule != None))

        now = utils.utcnow()
        outdated_queries = {}
        for query in queries:
            if should_schedule_next(query.latest_query_data.retrieved_at, now, query.schedule):
                key = "{}:{}".format(query.query_hash, query.data_source.id)
                outdated_queries[key] = query

        return outdated_queries.values()

    @classmethod
    def search(cls, term, groups):
        # TODO: This is very naive implementation of search, to be replaced with PostgreSQL full-text-search solution.
        where = (Query.name.like(u"%{}%".format(term)) |
                 Query.description.like(u"%{}%".format(term)))

        if term.isdigit():
            where |= Query.id == term

        where &= Query.is_archived == False
        where &= DataSourceGroup.group_id.in_([g.id for g in groups])
        query_ids = (
            db.session.query(Query.id).join(
                DataSourceGroup,
                Query.data_source_id == DataSourceGroup.data_source_id)
            .filter(where)).distinct()

        return db.session.query(Query).join(User, Query.user_id == User.id).filter(
            Query.id.in_(query_ids))

    @classmethod
    def recent(cls, groups, user_id=None, limit=20):
        query = (db.session.query(Query).join(User, Query.user_id == User.id)
                 .filter(Event.created_at > (db.func.current_date() - 7))
                 .join(Event, Query.id == Event.object_id.cast(db.Integer))
                 .join(DataSourceGroup, Query.data_source_id == DataSourceGroup.data_source_id)
                 .filter(
                     Event.action.in_(['edit', 'execute', 'edit_name',
                                       'edit_description', 'view_source']),
                     Event.object_id != None,
                     Event.object_type == 'query',
                     DataSourceGroup.group_id.in_([g.id for g in groups]),
                     Query.is_draft == False,
                     Query.is_archived == False)
                 .group_by(Event.object_id, Query.id, User.id)
                 .order_by(db.desc(db.func.count(0))))

        if user_id:
            query = query.filter(Event.user_id == user_id)

        query = query.limit(limit)

        return query

    def fork(self, user):
        query = self
        forked_query = Query()
        forked_query.name = 'Copy of (#{}) {}'.format(query.id, query.name)
        forked_query.user = user
        forked_list = ['org', 'data_source', 'latest_query_data', 'description', 'query', 'query_hash']
        for a in forked_list:
            setattr(forked_query, a, getattr(query, a))
        forked_query.save()

        forked_visualizations = []
        for v in query.visualizations:
            if v.type == 'TABLE':
                continue
            forked_v = v.to_dict()
            forked_v['options'] = v.options
            forked_v['query'] = forked_query
            forked_v.pop('id')
            forked_visualizations.append(forked_v)
        
        if len(forked_visualizations) > 0:
            with db.database.atomic():
                Visualization.insert_many(forked_visualizations).execute()
        return forked_query

    def pre_save(self, created):
        super(Query, self).pre_save(created)
        self.query_hash = utils.gen_query_hash(self.query)
        self._set_api_key()

        if self.last_modified_by is None:
            self.last_modified_by = self.user

    def post_save(self, created):
        if created:
            self._create_default_visualizations()

    def update_instance_tracked(self, changing_user, old_object=None, *args, **kwargs):
        self.version += 1
        self.update_instance(*args, **kwargs)
        # save Change record
        new_change = Change.save_change(user=changing_user, old_object=old_object, new_object=self)
        return new_change

    def tracked_save(self, changing_user, old_object=None, *args, **kwargs):
        self.version += 1
        self.save(*args, **kwargs)
        # save Change record
        new_change = Change.save_change(user=changing_user, old_object=old_object, new_object=self)
        return new_change

    def _create_default_visualizations(self):
        table_visualization = Visualization(query=self, name="Table",
                                            description='',
                                            type="TABLE", options="{}")
        table_visualization.save()

    def _set_api_key(self):
        if not self.api_key:
            self.api_key = hashlib.sha1(
                u''.join((str(time.time()), self.query, str(self.user_id), self.name)).encode('utf-8')).hexdigest()

    @property
    def runtime(self):
        return self.latest_query_data.runtime

    @property
    def retrieved_at(self):
        return self.latest_query_data.retrieved_at

    @property
    def groups(self):
        if self.data_source is None:
            return {}

        return self.data_source.groups

    def __unicode__(self):
        return unicode(self.id)

@listens_for(Query.query, 'set')
def gen_query_hash(target, val, oldval, initiator):
    target.query_hash = utils.gen_query_hash(val)

@listens_for(Query.user_id, 'set')
def query_last_modified_by(target, val, oldval, initiator):
    target.last_modified_by_id = val

@listens_for(SignallingSession, 'before_flush')
def create_defaults(session, ctx, *a):
    for obj in session.new:
        if isinstance(obj, Query):
            session.add(Visualization(query=obj, name="Table",
                                      description='',
                                      type="TABLE", options="{}"))

@listens_for(ChangeTrackingMixin, 'init')
def create_first_change(obj, args, kwargs):
    obj.record_changes(obj.user)



class AccessPermission(GFKBase, db.Model):
    id = Column(db.Integer, primary_key=True)
    # 'object' defined in GFKBase
    access_type = Column(db.String(255))
    grantor_id = Column(db.Integer, db.ForeignKey("users.id"))
    grantor = db.relationship(User, backref='grantor', foreign_keys=[grantor_id])
    grantee_id = Column(db.Integer, db.ForeignKey("users.id"))
    grantee = db.relationship(User, backref='grantee', foreign_keys=[grantee_id])

    __tablename__ = 'access_permissions'

    @classmethod
    def grant(cls, obj, access_type, grantee, grantor):
        return cls.get_or_create(object_type=obj._meta.db_table, object_id=obj.id, access_type=access_type, grantee=grantee, grantor=grantor)[0]

    @classmethod
    def revoke(cls, obj, grantee, access_type=None):
        query = cls._query(cls.delete(), obj, access_type, grantee)

        return query.execute()

    @classmethod
    def find(cls, obj, access_type=None, grantee=None, grantor=None):
        return cls._query(cls.select(cls), obj, access_type, grantee, grantor)

    @classmethod
    def exists(cls, obj, access_type, grantee):
        return cls.find(obj, access_type, grantee).count() > 0

    @classmethod
    def _query(cls, base_query, obj, access_type=None, grantee=None, grantor=None):
        q = base_query.where(cls.object_type == obj._meta.db_table) \
            .where(cls.object_id == obj.id)

        if access_type:
            q = q.where(AccessPermission.access_type == access_type)

        if grantee:
            q = q.where(AccessPermission.grantee == grantee)

        if grantor:
            q = q.where(AccessPermission.grantor == grantor)

        return q

    def to_dict(self):
        d = {
            'id': self.id,
            'object_id': self.object_id,
            'object_type': self.object_type,
            'access_type': self.access_type,
            'grantor': self.grantor_id,
            'grantee': self.grantee_id
        }
        return d


class Change(GFKBase, db.Model):
    id = Column(db.Integer, primary_key=True)
    # 'object' defined in GFKBase
    object_version = Column(db.Integer, default=0)
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User, backref='changes')
    change = Column(PseudoJSON)
    created_at = Column(db.DateTime(True), default=db.func.now())

    __tablename__ = 'changes'

    def to_dict(self, full=True):
        d = {
            'id': self.id,
            'object_id': self.object_id,
            'object_type': self.object_type,
            'change_type': self.change_type,
            'object_version': self.object_version,
            'change': self.change,
            'created_at': self.created_at
        }

        if full:
            d['user'] = self.user.to_dict()
        else:
            d['user_id'] = self.user_id

        return d

    @classmethod
    def log_change(cls, changed_by, obj):
        return cls.create(object=obj, object_version=obj.version, user=changed_by, change=obj.changes)

    @classmethod
    def last_change(cls, obj):
        return cls.select().where(cls.object_type==obj._meta.db_table, cls.object_id==obj.id).limit(1).first()


class Alert(TimestampMixin, db.Model):
    UNKNOWN_STATE = 'unknown'
    OK_STATE = 'ok'
    TRIGGERED_STATE = 'triggered'

    id = Column(db.Integer, primary_key=True)
    name = Column(db.String(255))
    query_id = Column(db.Integer, db.ForeignKey("queries.id"))
    query = db.relationship(Query, backref='alerts')
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User, backref='alerts')
    options = Column(PseudoJSON)
    state = Column(db.String(255), default=UNKNOWN_STATE)
    subscriptions = db.relationship("AlertSubscription", cascade="delete")
    last_triggered_at = Column(db.DateTime(True), nullable=True)
    rearm = Column(db.Integer, nullable=True)

    __tablename__ = 'alerts'

    @classmethod
    def all(cls, groups):
        return cls.select(Alert, User, Query)\
            .join(Query)\
            .join(DataSourceGroup, on=(Query.data_source==DataSourceGroup.data_source))\
            .where(DataSourceGroup.group << groups)\
            .switch(Alert)\
            .join(User)\
            .group_by(Alert, User, Query)

    @classmethod
    def get_by_id_and_org(cls, id, org):
        return cls.select(Alert, User, Query).join(Query).switch(Alert).join(User).where(cls.id==id, Query.org==org).get()

    def to_dict(self, full=True):
        d = {
            'id': self.id,
            'name': self.name,
            'options': self.options,
            'state': self.state,
            'last_triggered_at': self.last_triggered_at,
            'updated_at': self.updated_at,
            'created_at': self.created_at,
            'rearm': self.rearm
        }

        if full:
            d['query'] = self.query.to_dict()
            d['user'] = self.user.to_dict()
        else:
            d['query_id'] = self.query_id
            d['user_id'] = self.user_id

        return d

    def evaluate(self):
        data = json.loads(self.query.latest_query_data.data)
        # todo: safe guard for empty
        value = data['rows'][0][self.options['column']]
        op = self.options['op']

        if op == 'greater than' and value > self.options['value']:
            new_state = self.TRIGGERED_STATE
        elif op == 'less than' and value < self.options['value']:
            new_state = self.TRIGGERED_STATE
        elif op == 'equals' and value == self.options['value']:
            new_state = self.TRIGGERED_STATE
        else:
            new_state = self.OK_STATE

        return new_state

    def subscribers(self):
        return User.select().join(AlertSubscription).where(AlertSubscription.alert==self)

    @property
    def groups(self):
        return self.query.groups


def generate_slug(ctx):
    slug = utils.slugify(ctx.current_parameters['name'])
    tries = 1
    while db.session.query(Dashboard).filter(Dashboard.slug == slug).first() is not None:
        slug = utils.slugify(ctx.current_parameters['name']) + "_" + str(tries)
        tries += 1
    return slug


class Dashboard(ChangeTrackingMixin, TimestampMixin, BelongsToOrgMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    version = Column(db.Integer)
    org_id = Column(db.Integer, db.ForeignKey("organizations.id"))
    org = db.relationship(Organization, backref="dashboards")
    slug = Column(db.String(140), index=True, default=generate_slug)
    name = Column(db.String(100))
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User)
    # XXX replace with association table
    layout = Column(db.Text)
    dashboard_filters_enabled = Column(db.Boolean, default=False)
    is_archived = Column(db.Boolean, default=False, index=True)
    is_draft = Column(db.Boolean, default=False, index=True)

    __tablename__ = 'dashboards'
    __mapper_args__ = {
        "version_id_col": version
        }

    def to_dict(self, with_widgets=False, user=None):
        layout = json.loads(self.layout)

        if with_widgets:
            widget_list = Widget.select(Widget, Visualization, Query, User)\
                .where(Widget.dashboard == self.id)\
                .join(Visualization, join_type=peewee.JOIN_LEFT_OUTER)\
                .join(Query, join_type=peewee.JOIN_LEFT_OUTER)\
                .join(User, join_type=peewee.JOIN_LEFT_OUTER)

            widgets = {}

            for w in widget_list:
                if w.visualization_id is None:
                    widgets[w.id] = w.to_dict()
                elif user and has_access(w.visualization.query.groups, user, view_only):
                    widgets[w.id] = w.to_dict()
                else:
                    widgets[w.id] = project(w.to_dict(),
                                            ('id', 'width', 'dashboard_id', 'options', 'created_at', 'updated_at'))
                    widgets[w.id]['restricted'] = True

            # The following is a workaround for cases when the widget object gets deleted without the dashboard layout
            # updated. This happens for users with old databases that didn't have a foreign key relationship between
            # visualizations and widgets.
            # It's temporary until better solution is implemented (we probably should move the position information
            # to the widget).
            widgets_layout = []
            for row in layout:
                new_row = []
                for widget_id in row:
                    widget = widgets.get(widget_id, None)
                    if widget:
                        new_row.append(widget)

                widgets_layout.append(new_row)
        else:
            widgets_layout = None

        return {
            'id': self.id,
            'slug': self.slug,
            'name': self.name,
            'user_id': self.user_id,
            'layout': layout,
            'dashboard_filters_enabled': self.dashboard_filters_enabled,
            'widgets': widgets_layout,
            'is_archived': self.is_archived,
            'is_draft': self.is_draft,
            'updated_at': self.updated_at,
            'created_at': self.created_at,
            'version': self.version
        }

    @classmethod
    def all(cls, org, group_ids, user_id):
        query = (
            db.session.query(Dashboard)
            .outerjoin(Widget)
            .outerjoin(Visualization)
            .outerjoin(Query)
            .outerjoin(DataSourceGroup, Query.data_source_id == DataSourceGroup.data_source_id)
            .filter(
                Dashboard.is_archived == False,
                (DataSourceGroup.group_id.in_(group_ids) |
                 (Dashboard.user_id == user_id) |
                 ((Widget.dashboard != None) & (Widget.visualization == None))),
                Dashboard.org == org)
            .group_by(Dashboard.id))

        return query

    @classmethod
    def recent(cls, org, group_ids, user_id, for_user=False, limit=20):
        query = (db.session.query(Dashboard)
                 .outerjoin(Event, Dashboard.id == Event.object_id.cast(db.Integer))
                 .outerjoin(Widget)
                 .outerjoin(Visualization)
                 .outerjoin(Query)
                 .outerjoin(DataSourceGroup, Query.data_source_id == DataSourceGroup.data_source_id)
                 .filter(
                     Event.created_at > (db.func.current_date() - 7),
                     Event.action.in_(['edit', 'view']),
                     Event.object_id != None,
                     Event.object_type == 'dashboard',
                     Dashboard.org == org,
                     Dashboard.is_archived == False,
                     Dashboard.is_draft == False,
                     DataSourceGroup.group_id.in_(group_ids) |
                     (Dashboard.user_id == user_id) |
                     ((Widget.dashboard != None) & (Widget.visualization == None)))
                 .group_by(Event.object_id, Dashboard.id)
                 .order_by(db.desc(db.func.count(0))))


        if for_user:
            query = query.filter(Event.user_id == user_id)

        query = query.limit(limit)

        return query

    @classmethod
    def get_by_slug_and_org(cls, slug, org):
        return cls.get(cls.slug == slug, cls.org==org)

    def tracked_save(self, changing_user, old_object=None, *args, **kwargs):
        self.version += 1
        self.save(*args, **kwargs)
        # save Change record
        new_change = Change.save_change(user=changing_user, old_object=old_object, new_object=self)
        return new_change


    def __unicode__(self):
        return u"%s=%s" % (self.id, self.name)


class Visualization(TimestampMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    type = Column(db.String(100))
    query_id = Column(db.Integer, db.ForeignKey("queries.id"))
    query = db.relationship(Query, backref='visualizations')
    name = Column(db.String(255))
    description = Column(db.String(4096), nullable=True)
    options = Column(db.Text)

    __tablename__ = 'visualizations'

    def to_dict(self, with_query=True):
        d = {
            'id': self.id,
            'type': self.type,
            'name': self.name,
            'description': self.description,
            'options': json.loads(self.options),
            'updated_at': self.updated_at,
            'created_at': self.created_at
        }

        if with_query:
            d['query'] = self.query.to_dict()

        return d

    @classmethod
    def get_by_id_and_org(cls, visualization_id, org):
        return cls.select(Visualization, Query).join(Query).where(cls.id == visualization_id,
                                                                  Query.org == org).get()

    def __unicode__(self):
        return u"%s %s" % (self.id, self.type)


class Widget(TimestampMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    visualization_id = Column(db.Integer, db.ForeignKey('visualizations.id'), nullable=True)
    visualization = db.relationship(Visualization, backref='widgets')
    text = Column(db.Text, nullable=True)
    width = Column(db.Integer)
    options = Column(db.Text)
    dashboard_id = Column(db.Integer, db.ForeignKey("dashboards.id"), index=True)
    dashboard = db.relationship(Dashboard)

    # unused; kept for backward compatability:
    type = Column(db.String(100), nullable=True)
    query_id = Column(db.Integer, nullable=True)

    __tablename__ = 'widgets'

    def to_dict(self):
        d = {
            'id': self.id,
            'width': self.width,
            'options': json.loads(self.options),
            'dashboard_id': self.dashboard_id,
            'text': self.text,
            'updated_at': self.updated_at,
            'created_at': self.created_at
        }

        if self.visualization and self.visualization.id:
            d['visualization'] = self.visualization.to_dict()

        return d

    def __unicode__(self):
        return u"%s" % self.id

    @classmethod
    def get_by_id_and_org(cls, widget_id, org):
        return cls.select(cls, Dashboard).join(Dashboard).where(cls.id == widget_id, Dashboard.org == org).get()

#XXX produces SQLA warning, replace with association table
@listens_for(Widget, 'before_delete')
def widget_delete(mapper, connection, self):
    layout = json.loads(self.dashboard.layout)
    layout = map(lambda row: filter(lambda w: w != self.id, row), layout)
    layout = filter(lambda row: len(row) > 0, layout)
    self.dashboard.layout = json.dumps(layout)
    db.session.add(self.dashboard)


class Event(db.Model):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey("organizations.id"))
    org = db.relationship(Organization, backref="events")
    user_id = Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    user = db.relationship(User, backref="events")
    action = Column(db.String(255))
    # XXX replace with association table
    object_type = Column(db.String(255))
    object_id = Column(db.String(255), nullable=True)
    additional_properties = Column(db.Text, nullable=True)
    created_at = Column(db.DateTime(True), default=db.func.now())

    __tablename__ = 'events'

    def __unicode__(self):
        return u"%s,%s,%s,%s" % (self.user_id, self.action, self.object_type, self.object_id)

    @classmethod
    def record(cls, event):
        org_id = event.pop('org_id')
        user_id = event.pop('user_id', None)
        action = event.pop('action')
        object_type = event.pop('object_type')
        object_id = event.pop('object_id', None)

        created_at = datetime.datetime.utcfromtimestamp(event.pop('timestamp'))
        additional_properties = json.dumps(event)

        event = cls(org_id=org_id, user_id=user_id, action=action,
                    object_type=object_type, object_id=object_id,
                    additional_properties=additional_properties,
                    created_at=created_at)
        db.session.add(event)
        return event

class ApiKey(TimestampMixin, GFKBase, db.Model):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey("organizations.id"))
    org = db.relationship(Organization)
    api_key = Column(db.String(255), index=True, default=lambda: generate_token(40))
    active = Column(db.Boolean, default=True)
    #'object' provided by GFKBase
    created_by_id = Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship(User)

    __tablename__ = 'api_keys'
    __table_args__ = (db.Index('api_keys_object_type_object_id', 'object_type', 'object_id'),)

    @classmethod
    def get_by_api_key(cls, api_key):
        return cls.get(cls.api_key==api_key, cls.active==True)

    @classmethod
    def get_by_object(cls, object):
        return cls.select().where(cls.object_type==object._meta.db_table, cls.object_id==object.id, cls.active==True).first()

    @classmethod
    def create_for_object(cls, object, user):
        return cls.create(org=user.org, object=object, created_by=user)


class NotificationDestination(BelongsToOrgMixin, db.Model):

    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey("organizations.id"))
    org = db.relationship(Organization, backref="notification_destinations")
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User, backref="notification_destinations")
    name = Column(db.String(255))
    type = Column(db.String(255))
    options = Column(Configuration)
    created_at = Column(db.DateTime(True), default=db.func.now())
    __tablename__ = 'notification_destinations'
    __table_args__ = (db.Index('notification_destinations_org_id_name', 'org_id',
                               'name', unique=True),)

    def to_dict(self, all=False):
        d = {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'icon': self.destination.icon()
        }

        if all:
            schema = get_configuration_schema_for_destination_type(self.type)
            self.options.set_schema(schema)
            d['options'] = self.options.to_dict(mask_secrets=True)

        return d

    def __unicode__(self):
        return self.name

    @property
    def destination(self):
        return get_destination(self.type, self.options)

    @classmethod
    def all(cls, org):
        notification_destinations = cls.select().where(cls.org==org).order_by(cls.id.asc())

        return notification_destinations

    def notify(self, alert, query, user, new_state, app, host):
        schema = get_configuration_schema_for_destination_type(self.type)
        self.options.set_schema(schema)
        return self.destination.notify(alert, query, user, new_state,
                                       app, host, self.options)


class AlertSubscription(TimestampMixin, db.Model):
    id = Column(db.Integer, primary_key=True)
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User)
    destination_id = Column(db.Integer,
                               db.ForeignKey("notification_destinations.id"),
                               nullable=True)
    destination = db.relationship(NotificationDestination)
    alert_id = Column(db.Integer, db.ForeignKey("alerts.id"))
    alert = db.relationship(Alert, back_populates="subscriptions")

    __tablename__ = 'alert_subscriptions'
    __table_args__ = (db.Index('alert_subscriptions_destination_id_alert_id',
                               'destination_id', 'alert_id', unique=True),)

    def to_dict(self):
        d = {
            'id': self.id,
            'user': self.user.to_dict(),
            'alert_id': self.alert_id
        }

        if self.destination:
            d['destination'] = self.destination.to_dict()

        return d

    @classmethod
    def all(cls, alert_id):
        return AlertSubscription.select(AlertSubscription, User).join(User).where(AlertSubscription.alert==alert_id)

    def notify(self, alert, query, user, new_state, app, host):
        if self.destination:
            return self.destination.notify(alert, query, user, new_state,
                                           app, host)
        else:
            # User email subscription, so create an email destination object
            config = {'addresses': self.user.email}
            schema = get_configuration_schema_for_destination_type('email')
            options = ConfigurationContainer(config, schema)
            destination = get_destination('email', options)
            return destination.notify(alert, query, user, new_state, app, host, options)


class QuerySnippet(TimestampMixin, db.Model, BelongsToOrgMixin):
    id = Column(db.Integer, primary_key=True)
    org_id = Column(db.Integer, db.ForeignKey("organizations.id"))
    org = db.relationship(Organization, backref="query_snippets")
    trigger = Column(db.String(255), unique=True)
    description = Column(db.Text)
    user_id = Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship(User, backref="query_snippets")
    snippet = Column(db.Text)
    __tablename__ = 'query_snippets'

    @classmethod
    def all(cls, org):
        return cls.select().where(cls.org==org)

    def to_dict(self):
        d = {
            'id': self.id,
            'trigger': self.trigger,
            'description': self.description,
            'snippet': self.snippet,
            'user': self.user.to_dict(),
            'updated_at': self.updated_at,
            'created_at': self.created_at
        }

        return d

_gfk_types = {'queries': Query, 'dashboards': Dashboard}


def init_db():
    default_org = Organization(name="Default", slug='default', settings={})
    admin_group = Group(name='admin', permissions=['admin', 'super_admin'], org=default_org, type=Group.BUILTIN_GROUP)
    default_group = Group(name='default', permissions=Group.DEFAULT_PERMISSIONS, org=default_org, type=Group.BUILTIN_GROUP)

    db.session.add_all([default_org, admin_group, default_group])
    #XXX remove after fixing User.group_ids
    db.session.commit()
    return default_org, admin_group, default_group


def create_db(create_tables, drop_tables):
    # TODO: use these methods directly
    if drop_tables:
        db.session.rollback()
        db.drop_all()

    if create_tables:
        db.create_all()
