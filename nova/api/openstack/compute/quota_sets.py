# Copyright 2011 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_utils import strutils
import six
import six.moves.urllib.parse as urlparse
import webob

from nova.api.openstack.compute.schemas import quota_sets
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api import validation
from nova.db.sqlalchemy import api as sqlalchemy_api
from nova import context
from nova import db
from nova import exception
from nova.i18n import _
from nova import objects
from nova import quota
from nova import utils


ALIAS = "os-quota-sets"
QUOTAS = quota.QUOTAS
KEYSTONE = context.KEYSTONE
authorize = extensions.os_compute_authorizer(ALIAS)


class QuotaSetsController(wsgi.Controller):

    def _format_quota_set(self, project_id, quota_set):
        """Convert the quota object to a result dict."""
        if project_id:
            result = dict(id=str(project_id))
        else:
            result = {}

        for resource in QUOTAS.resources:
            if resource in quota_set:
                result[resource] = quota_set[resource]
        return dict(quota_set=result)

    def _validate_quota_hierarchy(self, quota, key, project_quotas=None,
                                  parent_project_quotas=None):
	limit = utils.validate_integer(quota[key], key, min_value=-1,
                                       max_value=db.MAX_INT)
        # NOTE: -1 is a flag value for unlimited
        if limit < -1:
            msg = (_("Quota limit %(limit)s for %(key)s "
                     "must be -1 or greater.") %
                   {'limit': limit, 'key': key})
            raise webob.exc.HTTPBadRequest(explanation=msg)

        if parent_project_quotas:
            free_quota = (parent_project_quotas[key]['limit'] -
                          parent_project_quotas[key]['in_use'] -
                          parent_project_quotas[key]['reserved'] -
                          parent_project_quotas[key]['allocated'])

            current = 0
            if project_quotas.get(key):
                current = project_quotas[key]['limit']

            if limit - current > free_quota:
                msg = _("Free quota available is %s.") % free_quota
                raise webob.exc.HTTPBadRequest(explanation=msg)
        return limit

    def _validate_quota_limit(self, resource, limit, minimum, maximum):
        # NOTE: -1 is a flag value for unlimited
        if limit < -1:
            msg = (_("Quota limit %(limit)s for %(resource)s "
                     "must be -1 or greater.") %
                   {'limit': limit, 'resource': resource})
            raise webob.exc.HTTPBadRequest(explanation=msg)

        def conv_inf(value):
            return float("inf") if value == -1 else value

        if conv_inf(limit) < conv_inf(minimum):
            msg = (_("Quota limit %(limit)s for %(resource)s must "
                     "be greater than or equal to already used and "
                     "reserved %(minimum)s.") %
                   {'limit': limit, 'resource': resource, 'minimum': minimum})
            raise webob.exc.HTTPBadRequest(explanation=msg)
        if conv_inf(limit) > conv_inf(maximum):
            msg = (_("Quota limit %(limit)s for %(resource)s must be "
                     "less than or equal to %(maximum)s.") %
                   {'limit': limit, 'resource': resource, 'maximum': maximum})
            raise webob.exc.HTTPBadRequest(explanation=msg)

    def _get_quotas(self, context, id, user_id=None, usages=False,
                    parent_project_id=None):
        if user_id:
            values = QUOTAS.get_user_quotas(context, id, user_id,
                                            usages=usages)
        else:
            values = QUOTAS.get_project_quotas(
                context, id, usages=usages,
                parent_project_id=parent_project_id)

        if usages:
            return values
        else:
            return {k: v['limit'] for k, v in values.items()}

    def _is_descendant(self, target_project_id, subtree):
        if subtree is not None:
            for key, value in subtree.items():
                if key == target_project_id:
                    return True
                if self._is_descendant(target_project_id, value):
                    return True
        return False

    def _authorize_update_or_delete(self, context_project,
                                    target_project_id,
                                    parent_id):
        """Checks if update or delete are allowed in the current hierarchy.

        With hierarchical projects, only the admin of the parent or the root
        project has privilege to perform quota update and delete operations.

        :param context_project: The project in which the user is scoped to.
        :param target_project_id: The id of the project in which the
                                  user want to perform an update or
                                  delete operation.
        :param parent_id: The parent id of the project in which the user
                          want to perform an update or delete operation.
        """
        if context_project.parent_id and parent_id != context_project.id:
            msg = _("Update and delete quota operations can only be made "
                    "by an admin of immediate parent or by the CLOUD admin.")
            raise webob.exc.HTTPForbidden(explanation=msg)

        if context_project.id != target_project_id:
            if not self._is_descendant(target_project_id,
                                       context_project.subtree):
                msg = _("Update and delete quota operations can only be made "
                        "to projects in the same hierarchy of the project in "
                        "which users are scoped to.")
                raise webob.exc.HTTPForbidden(explanation=msg)
        else:
            msg = _("Update and delete quota operations can only be made "
                    "by an admin of immediate parent or by the CLOUD admin.")
            raise webob.exc.HTTPForbidden(explanation=msg)

    def _authorize_show(self, context_project, target_project):
        """Checks if show is allowed in the current hierarchy.

        With hierarchical projects, are allowed to perform quota show operation
        users with admin role in, at least, one of the following projects: the
        current project; the immediate parent project; or the root project.

        :param context_project: The project in which the user
                                is scoped to.
        :param target_project: The project in which the user wants
                               to perform a show operation.
        """
        if target_project.parent_id:
            if target_project.id != context_project.id:
                if not self._is_descendant(target_project.id,
                                           context_project.subtree):
                    msg = _("Show operations can only be made to projects in "
                            "the same hierarchy of the project in which users "
                            "are scoped to.")
                    raise webob.exc.HTTPForbidden(explanation=msg)
                if context_project.id != target_project.parent_id:
                    if context_project.parent_id:
                        msg = _("Only users with token scoped to immediate "
                                "parents or root projects are allowed to see "
                                "its children quotas.")
                        raise webob.exc.HTTPForbidden(explanation=msg)
        elif context_project.parent_id:
            msg = _("An user with a token scoped to a subproject is not "
                    "allowed to see the quota of its parents.")
            raise webob.exc.HTTPForbidden(explanation=msg)

    @extensions.expected_errors(403)
    def show(self, req, id):
        """Show quota for a particular tenant

        This works for hierarchical and non-hierarchical projects. For
        hierarchical projects admin of current project, immediate
        parent of the project or the CLOUD admin are able to perform
        a show.

        :param req: request
        :param id: target project id that needs to be updated
        """
        context = req.environ['nova.context']
        authorize(context, action='show', target={'project_id': id})
        params = urlparse.parse_qs(req.environ.get('QUERY_STRING', ''))
        user_id = params.get('user_id', [None])[0]
        target_project_id = id

        # With hierarchical projects, only the admin of the current project or
        # the root project has privilege to perform quota show operations.
        context_project = KEYSTONE.get_project(context, context.project_id,
                                               subtree=True)
        target_project = KEYSTONE.get_project(context, target_project_id)

        self._authorize_show(context_project, target_project)
        quotas = self._get_quotas(context, target_project_id, user_id=user_id,
            parent_project_id=target_project.parent_id)
        return self._format_quota_set(target_project_id, quotas)

    @extensions.expected_errors(())
    def detail(self, req, id):
        context = req.environ['nova.context']
        authorize(context, action='detail', target={'project_id': id})
        user_id = req.GET.get('user_id', None)
        return self._format_quota_set(id, self._get_quotas(context, id,
                                                           user_id=user_id,
                                                           usages=True))

    @extensions.expected_errors((400, 403))
    @validation.schema(quota_sets.update)
    def update(self, req, id, body):
        """Update Quota for a particular tenant

        This works for hierarchical and non-hierarchical projects. For
        hierarchical projects only immediate parent admin or the
        CLOUD admin are able to perform an update.

        :param req: request
        :param id: target project id that needs to be updated
        :param body: key, value pair that that will be
                     applied to the resources if the update
                     succeeds
        """
        context = req.environ['nova.context']
        authorize(context, action='update', target={'project_id': id})
        target_project_id = id
        params = urlparse.parse_qs(req.environ.get('QUERY_STRING', ''))
        user_id = params.get('user_id', [None])[0]

        quota_set = body['quota_set']
        force_update = strutils.bool_from_string(quota_set.get('force',
                                                               'False'))

        # Get the parent_id of the target project to verify whether we are
        # dealing with hierarchical namespace or non-hierarchical namespace.
        target_project = KEYSTONE.get_project(context, target_project_id)
        parent_project_id = target_project.parent_id

        if parent_project_id:
            # Get the children of the project which the token is scoped to in
            # order to know if the target_project is in its hierarchy.
            context_project = KEYSTONE.get_project(context,
                                                   context.project_id,
                                                   subtree=True)
            self._authorize_update_or_delete(context_project,
                                             target_project.id,
                                             parent_project_id)
            parent_project_quotas = QUOTAS.get_project_quotas(
                context, parent_project_id, parent_project_id=parent_project_id)

        settable_quotas = QUOTAS.get_settable_quotas(context,
                                                     target_project_id,
                                                     user_id=user_id)

        # NOTE(dims): Pass #1 - In this loop for quota_set.items(), we validate
        # min/max values and bail out if any of the items in the set is bad.
        valid_quotas = {}
        allocated_quotas = {}
        quota_values = QUOTAS.get_project_quotas(context, target_project_id,
                                                 defaults=False)
        for key, value in six.iteritems(body['quota_set']):
            if key == 'force' or (not value and value != 0):
                continue
            # validate whether already used and reserved exceeds the new
            # quota, this check will be ignored if admin want to force
            # update
            value = int(value)
            if not force_update:
                minimum = settable_quotas[key]['minimum']
                maximum = settable_quotas[key]['maximum']
	        self._validate_quota_limit(key, value, minimum, maximum)

            if parent_project_id:
                value = self._validate_quota_hierarchy(body['quota_set'], key,
                                               quota_values,
                                               parent_project_quotas)
                allocated_quotas[key] = (
                    parent_project_quotas[key].get('allocated', 0) + value)

	    valid_quotas[key] = value

        # NOTE(dims): Pass #2 - At this point we know that all the
        # values are correct and we can iterate and update them all in one
        # shot without having to worry about rolling back etc as we have done
        # the validation up front in the loop above.
        for key, value in valid_quotas.items():
            try:
                objects.Quotas.create_limit(context, target_project_id,
                                            key, value, user_id=user_id)
            except exception.QuotaExists:
                objects.Quotas.update_limit(context, target_project_id,
                                            key, value, user_id=user_id)
        # If hierarchical projects, update child's quota first
        # and then parents quota. In future this needs to be an
        # atomic operation.
        if parent_project_id:
            if key in allocated_quotas.keys():
                try:
                    sqlalchemy_api.quota_allocated_update(context,
                                                          parent_project_id,
                                                          key,
                                                          allocated_quotas[key])
                except exception.ProjectQuotaNotFound:
		    print 'pai nao tem quota %%%%%%%%%'
                    parent_limit = parent_project_quotas[key]['limit']
		    print 'parent limit %s' % parent_limit
                    sqlalchemy_api.quota_create(context,
                                                parent_project_id,
                                                key,
                                                parent_limit,
                                                allocated=allocated_quotas[key])

        # Note(gmann): Removed 'id' from update's response to make it same
        # as V2. If needed it can be added with microversion.
        return self._format_quota_set(None, self._get_quotas(context,
                                                             target_project_id,
                                                             user_id=user_id))

    @extensions.expected_errors(())
    def defaults(self, req, id):
        context = req.environ['nova.context']
        authorize(context, action='defaults', target={'project_id': id})
        try:
            project = KEYSTONE.get_project(context, id)
            parent_project_id = project.parent_id
        except exception.Forbidden:
            # NOTE(ericksonsantos): Keystone API v2 requires admin permissions
            # for project_get method. We ignore Forbidden exception for
            # non-admin users.
            parent_project_id = context.project_id
        values = QUOTAS.get_defaults(
            context, parent_project_id=parent_project_id)
        return self._format_quota_set(id, values)

    # TODO(oomichi): Here should be 204(No Content) instead of 202 by v2.1
    # +microversions because the resource quota-set has been deleted completely
    # when returning a response.
    @extensions.expected_errors(403)
    @wsgi.response(202)
    def delete(self, req, id):
        """Delete Quota for a particular tenant.

        This works for hierarchical and non-hierarchical projects. For
        hierarchical projects only immediate parent admin or the
        CLOUD admin are able to perform a delete.

        :param req: request
        :param id: target project id that needs to be updated
        """
        context = req.environ['nova.context']
        authorize(context, action='delete', target={'project_id': id})
        # Get the parent_id of the target project to verify whether we are
        # dealing with hierarchical namespace or non-hierarchical namespace.
        target_project = KEYSTONE.get_project(context, id)
        parent_id = target_project.parent_id
        params = urlparse.parse_qs(req.environ.get('QUERY_STRING', ''))
        user_id = params.get('user_id', [None])[0]
        try:
            project_quotas = QUOTAS.get_project_quotas(
                context, target_project.id, usages=True,
                parent_project_id=parent_id, defaults=False)
        except exception.NotAuthorized:
            raise webob.exc.HTTPForbidden()

        # If the project which is being deleted has allocated part of its quota
        # to its subprojects, then subprojects' quotas should be deleted first.
        for key, value in project_quotas.items():
                if 'allocated' in project_quotas[key].keys():
                    if project_quotas[key]['allocated'] != 0:
                        msg = _("About to delete child projects having "
                                "non-zero quota. This should not be performed")
                        raise webob.exc.HTTPBadRequest(explanation=msg)

        if parent_id:
            # Get the children of the project which the token is scoped to in
            # order to know if the target_project is in its hierarchy.
            context_project = KEYSTONE.get_project(context,
                                                   context.project_id,
                                                   subtree=True)
            self._authorize_update_or_delete(context_project,
                                             target_project.id,
                                             parent_id)
            parent_project_quotas = QUOTAS.get_project_quotas(
                context, parent_id, parent_project_id=parent_id)

            # Delete child quota first and later update parent's quota.
            try:
                QUOTAS.destroy_all_by_project(context, target_project.id)
            except exception.AdminRequired:
                raise webob.exc.HTTPForbidden()

            # Update the allocated of the parent
            for key, value in project_quotas.items():
                project_hard_limit = project_quotas[key]['limit']
                parent_allocated = parent_project_quotas[key]['allocated']
                parent_allocated -= project_hard_limit
                sqlalchemy_api.quota_allocated_update(context, parent_id, key,
                                                      parent_allocated)
        elif user_id:
            QUOTAS.destroy_all_by_project_and_user(context,
                                                   id, user_id)
        else:
            QUOTAS.destroy_all_by_project(context, id)


class QuotaSets(extensions.V21APIExtensionBase):
    """Quotas management support."""

    name = "Quotas"
    alias = ALIAS
    version = 1

    def get_resources(self):
        resources = []

        res = extensions.ResourceExtension(ALIAS,
                                            QuotaSetsController(),
                                            member_actions={'defaults': 'GET',
                                                            'detail': 'GET'})
        resources.append(res)

        return resources

    def get_controller_extensions(self):
        return []
