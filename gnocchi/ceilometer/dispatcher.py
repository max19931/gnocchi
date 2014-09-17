#
# Copyright 2014 eNovance
#
# Authors: Julien Danjou <julien@danjou.info>
#          Mehdi Abaakouk <mehdi.abaakouk@enovance.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
from __future__ import absolute_import

import itertools
import json
import operator

from ceilometer import dispatcher
from ceilometer.openstack.common.gettextutils import _
from ceilometer.openstack.common import log
from oslo.config import cfg
import requests
import six
import stevedore.dispatch

LOG = log.getLogger(__name__)

dispatcher_opts = [
    cfg.StrOpt('url',
               default="http://localhost:8041",
               help='URL to Gnocchi.'),
    cfg.StrOpt('archive_policy',
               default="low",
               help='The archive policy to use when the dispatcher '
               'create a new entity.')
]

cfg.CONF.register_opts(dispatcher_opts, group="dispatcher_gnocchi")

CREATE_ENTITY = 'create_entity'
CREATE_RESOURCE = 'create_resource'


class UnexpectedWorkflowError(Exception):
    pass


class NoSuchEntity(Exception):
    pass


class EntityAlreadyExists(Exception):
    pass


class NoSuchResource(Exception):
    pass


class ResourceAlreadyExists(Exception):
    pass


def log_and_ignore_unexpected_workflow_error(func):
    def log_and_ignore(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except UnexpectedWorkflowError as e:
            LOG.error(six.text_type(e))
    return log_and_ignore


class GnocchiDispatcher(dispatcher.Base):
    def __init__(self, conf):
        super(GnocchiDispatcher, self).__init__(conf)
        self.gnocchi_url = conf.dispatcher_gnocchi.url
        self.gnocchi_archive_policy = {
            'archive_policy':
            cfg.CONF.dispatcher_gnocchi.archive_policy
        }
        self.mgmr = stevedore.dispatch.DispatchExtensionManager(
            'gnocchi.ceilometer.resource', lambda x: True,
            invoke_on_load=True)

    def record_metering_data(self, data):
        # FIXME(sileht): This method bulk the processing of samples
        # grouped by resource_id and entity_name but this is not
        # efficient yet because the data received here doesn't often
        # contains a lot of different kind of samples
        # So perhaps the next step will be to pool the received data from
        # message bus.

        resource_grouped_samples = itertools.groupby(
            data, key=operator.itemgetter('resource_id'))

        for resource_id, samples_of_resource in resource_grouped_samples:
            resource_need_to_be_updated = True

            entity_grouped_samples = itertools.groupby(
                list(samples_of_resource),
                key=operator.itemgetter('counter_name'))
            for entity_name, samples in entity_grouped_samples:
                for ext in self.mgmr:
                    if entity_name in ext.obj.get_entities_names():
                        self._process_samples(
                            ext, resource_id, entity_name, list(samples),
                            resource_need_to_be_updated)

                # FIXME(sileht): Does it reasonable to skip the resource
                # update here ? Does differents kind of counter_name
                # can have different metadata set ?
                # (ie: one have only flavor_id, and an other one have only
                # image_ref ?)
                #
                # resource_need_to_be_updated = False

    @log_and_ignore_unexpected_workflow_error
    def _process_samples(self, ext, resource_id, entity_name, samples,
                         resource_need_to_be_updated):

        resource_type = ext.name
        measure_attributes = [{'timestamp': sample['timestamp'],
                               'value': sample['counter_volume']}
                              for sample in samples]

        try:
            self._post_measure(resource_type, resource_id, entity_name,
                               measure_attributes)
        except NoSuchEntity:
            # NOTE(sileht): we try first to create the resource, because
            # they more chance that the resource doesn't exists than the entity
            # is missing, the should be reduce the number of resource API call
            resource_attributes = self._get_resource_attributes(
                ext, resource_id, entity_name, samples)
            try:
                self._create_resource(resource_type, resource_id,
                                      resource_attributes)
            except ResourceAlreadyExists:
                try:
                    self._create_entity(resource_type, resource_id,
                                        entity_name)
                except EntityAlreadyExists:
                    # NOTE(sileht): Just ignore the entity have been created in
                    # the meantime.
                    pass
            else:
                # No need to update it we just created it
                # with everything we need
                resource_need_to_be_updated = False

            # NOTE(sileht): we retry to post the measure but if it fail we
            # don't catch the exception to just log it and continue to process
            # other samples
            self._post_measure(resource_type, resource_id, entity_name,
                               measure_attributes)

        if resource_need_to_be_updated:
            resource_attributes = self._get_resource_attributes(
                ext, resource_id, entity_name, samples, for_update=True)
            self._update_resource(resource_type, resource_id,
                                  resource_attributes)

    def _get_resource_attributes(self, ext, resource_id, entity_name, samples,
                                 for_update=False):
        # FIXME(sileht): Should I merge attibutes of all samples ?
        # Or keep only the last one is sufficient ?
        attributes = ext.obj.get_resource_extra_attributes(
            samples[-1])
        if not for_update:
            attributes["id"] = resource_id
            attributes["user_id"] = samples[-1]['user_id']
            attributes["project_id"] = samples[-1]['project_id']
            attributes["entities"] = dict(
                (entity_name, self.gnocchi_archive_policy)
                for entity_name in ext.obj.get_entities_names()
            )
        return attributes

    def _post_measure(self, resource_type, resource_id, entity_name,
                      measure_attributes):
        r = requests.post("%s/v1/resource/%s/%s/entity/%s/measures"
                          % (self.gnocchi_url, resource_type, resource_id,
                             entity_name),
                          headers={'Content-Type': "application/json"},
                          data=json.dumps(measure_attributes))
        if r.status_code == 404:
            LOG.debug(_("The entity %(entity_name)s of "
                        "resource %(resource_id)s doesn't exists"
                        "%(status_code)d"),
                      {'entity_name': entity_name,
                       'resource_id': resource_id,
                       'status_code': r.status_code})
            raise NoSuchEntity
        elif int(r.status_code / 100) != 2:
            raise UnexpectedWorkflowError(
                _("Fail to post measure on entity %(entity_name)s of "
                  "resource %(resource_id)s with status: "
                  "%(status_code)d: %(msg)s") %
                {'entity_name': entity_name,
                 'resource_id': resource_id,
                 'status_code': r.status_code,
                 'msg': r.text})
        else:
            LOG.debug("Measure posted on entity %s of resource %s",
                      entity_name, resource_id)

    def _create_resource(self, resource_type, resource_id,
                         resource_attributes):
        r = requests.post("%s/v1/resource/%s"
                          % (self.gnocchi_url, resource_type),
                          headers={'Content-Type': "application/json"},
                          data=json.dumps(resource_attributes))
        if r.status_code == 409:
            LOG.debug("Resource %s already exists", resource_id)
            raise ResourceAlreadyExists

        elif int(r.status_code / 100) != 2:
            raise UnexpectedWorkflowError(
                _("Resource %(resource_id)s creation failed with "
                  "status: %(status_code)d: %(msg)s") %
                {'resource_id': resource_id,
                 'status_code': r.status_code,
                 'msg': r.text})
        else:
            LOG.debug("Resource %s created", resource_id)

    def _update_resource(self, resource_type, resource_id,
                         resource_attributes):
        r = requests.patch(
            "%s/v1/resource/%s/%s"
            % (self.gnocchi_url, resource_type, resource_id),
            headers={'Content-Type': "application/json"},
            data=json.dumps(resource_attributes))

        if int(r.status_code / 100) != 2:
            raise UnexpectedWorkflowError(
                _("Resource %(resource_id)s update failed with "
                  "status: %(status_code)d: %(msg)s") %
                {'resource_id': resource_id,
                 'status_code': r.status_code,
                 'msg': r.text})
        else:
            LOG.debug("Resource %s updated", resource_id)

    def _create_entity(self, resource_type, resource_id, entity_name):
        params = {entity_name: self.gnocchi_archive_policy}
        r = requests.post("%s/v1/resource/%s/%s/entity"
                          % (self.gnocchi_url, resource_type,
                             resource_id),
                          headers={'Content-Type': "application/json"},
                          data=json.dumps(params))
        if r.status_code == 409:
            LOG.debug("Entity %s of resource %s already exists",
                      entity_name, resource_id)
            raise EntityAlreadyExists

        elif int(r.status_code / 100) != 2:
            raise UnexpectedWorkflowError(
                _("Fail to create entity %(entity_name)s of "
                  "resource %(resource_id)s with status: "
                  "%(status_code)d: %(msg)s") %
                {'entity_name': entity_name,
                 'resource_id': resource_id,
                 'status_code': r.status_code,
                 'msg': r.text})
        else:
            LOG.debug("Entity %s of resource %s created",
                      entity_name, resource_id)

    @staticmethod
    def record_events(events):
        raise NotImplementedError
