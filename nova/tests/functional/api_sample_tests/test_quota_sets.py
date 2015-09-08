# Copyright 2012 Nebula, Inc.
# Copyright 2013 IBM Corp.
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

import mock
import uuid

from oslo_config import cfg
from nova.tests.functional.api_sample_tests import api_sample_base
from nova import context

CONF = cfg.CONF
CONF.import_opt('osapi_compute_extension',
                'nova.api.openstack.compute.legacy_v2.extensions')


class QuotaSetsSampleJsonTests(api_sample_base.ApiSampleTestBaseV21):
    ADMIN_API = True
    extension_name = "os-quota-sets"

    def setUp(self):
        super(QuotaSetsSampleJsonTests, self).setUp()
        self._create_project_hierarchy()

    class FakeProject(object):

        def __init__(self, id='foo', parent_id=None):
            self.id = id
            self.parent_id = parent_id
            self.subtree = None

    def _get_flags(self):
        f = super(QuotaSetsSampleJsonTests, self)._get_flags()
        f['osapi_compute_extension'] = CONF.osapi_compute_extension[:]
        f['osapi_compute_extension'].append('nova.api.openstack.compute.'
                                    'contrib.server_group_quotas.'
                                    'Server_group_quotas')
        f['osapi_compute_extension'].append('nova.api.openstack.compute.'
                                    'contrib.quotas.Quotas')
        f['osapi_compute_extension'].append('nova.api.openstack.compute.'
                                    'contrib.extended_quotas.Extended_quotas')
        f['osapi_compute_extension'].append('nova.api.openstack.compute.'
                                    'contrib.user_quotas.User_quotas')
        return f

    def _create_project_hierarchy(self):
        """Sets an environment used for nested quotas tests.
        Create a project hierarchy such as follows:
        +-----------+
        |           |
        |     A     |
        |    / \    |
        |   B   C   |
        |  /        |
        | D         |
        +-----------+
        """
        self.A = self.FakeProject(id=uuid.uuid4().hex, parent_id=None)
        self.B = self.FakeProject(id=uuid.uuid4().hex, parent_id=self.A.id)
        self.C = self.FakeProject(id=uuid.uuid4().hex, parent_id=self.A.id)
        self.D = self.FakeProject(id=uuid.uuid4().hex, parent_id=self.B.id)

        # update projects subtrees
        self.B.subtree = {self.D.id: self.D.subtree}
        self.A.subtree = {self.B.id: self.B.subtree, self.C.id: self.C.subtree}

        # project_by_id attribute is used to recover a project based on its id.
        self.project_by_id = {self.A.id: self.A, self.B.id: self.B,
                              self.C.id: self.C, self.D.id: self.D}

    def get_project(self, context, id, subtree=False):
        return self.project_by_id.get(id, self.FakeProject())

    def test_show_quotas(self):
        context.KEYSTONE.get_project = mock.Mock()
        context.KEYSTONE.get_project.side_effect = self.get_project
        # Get api sample to show quotas.
        response = self._do_get('os-quota-sets/fake_tenant')
        self._verify_response('quotas-show-get-resp', {}, response, 200)

    def test_show_quotas_defaults(self):
        context.KEYSTONE.get_project = mock.Mock()
        context.KEYSTONE.get_project.side_effect = self.get_project
        # Get api sample to show quotas defaults.
        response = self._do_get('os-quota-sets/fake_tenant/defaults')
        self._verify_response('quotas-show-defaults-get-resp',
                              {}, response, 200)

    def test_update_quotas(self):
        context.KEYSTONE.get_project = mock.Mock()
        context.KEYSTONE.get_project.side_effect = self.get_project
        # Get api sample to update quotas.
        response = self._do_put('os-quota-sets/fake_tenant',
                                'quotas-update-post-req',
                                {})
        self._verify_response('quotas-update-post-resp', {}, response, 200)

    def test_delete_quotas(self):
        context.KEYSTONE.get_project = mock.Mock()
        context.KEYSTONE.get_project.side_effect = self.get_project
        # Get api sample to delete quota.
        response = self._do_delete('os-quota-sets/fake_tenant')
        self.assertEqual(202, response.status_code)
        self.assertEqual('', response.content)

    def test_update_quotas_force(self):
        context.KEYSTONE.get_project = mock.Mock()
        context.KEYSTONE.get_project.side_effect = self.get_project
        # Get api sample to update quotas.
        response = self._do_put('os-quota-sets/fake_tenant',
                                'quotas-update-force-post-req',
                                {})
        return self._verify_response('quotas-update-force-post-resp', {},
                                     response, 200)

    def test_show_quotas_for_user(self):
        context.KEYSTONE.get_project = mock.Mock()
        context.KEYSTONE.get_project.side_effect = self.get_project
        # Get api sample to show quotas for user.
        response = self._do_get('os-quota-sets/fake_tenant?user_id=1')
        self._verify_response('user-quotas-show-get-resp', {}, response, 200)

    def test_delete_quotas_for_user(self):
        context.KEYSTONE.get_project = mock.Mock()
        context.KEYSTONE.get_project.side_effect = self.get_project
        response = self._do_delete('os-quota-sets/fake_tenant?user_id=1')
        self.assertEqual(202, response.status_code)
        self.assertEqual('', response.content)

    def test_update_quotas_for_user(self):
        context.KEYSTONE.get_project = mock.Mock()
        context.KEYSTONE.get_project.side_effect = self.get_project
        # Get api sample to update quotas for user.
        response = self._do_put('os-quota-sets/fake_tenant?user_id=1',
                                'user-quotas-update-post-req',
                                {})
        return self._verify_response('user-quotas-update-post-resp', {},
                                     response, 200)
