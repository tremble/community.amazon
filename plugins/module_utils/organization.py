# Copyright: Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import json

try:
    import botocore
except ImportError:
    pass  # caught by AnsibleAWSModule

from ansible.module_utils.common.dict_transformations import camel_dict_to_snake_dict
from ansible_collections.amazon.aws.plugins.module_utils.core import is_boto3_error_code
from ansible_collections.amazon.aws.plugins.module_utils.ec2 import AWSRetry
from ansible_collections.amazon.aws.plugins.module_utils.ec2 import ansible_dict_to_boto3_tag_list
from ansible_collections.amazon.aws.plugins.module_utils.ec2 import boto3_tag_list_to_ansible_dict
from ansible_collections.amazon.aws.plugins.module_utils.ec2 import compare_aws_tags
from ansible_collections.amazon.aws.plugins.module_utils.ec2 import compare_policies


class Policies(object):

    _TYPE_MAPPING = {
        None: 'SERVICE_CONTROL_POLICY',
        'service_control': 'SERVICE_CONTROL_POLICY',
        'aiservices_opt_out': 'AISERVICES_OPT_OUT_POLICY',
        'backup': 'BACKUP_POLICY',
        'tag': 'TAG_POLICY',
    }

    def normalize_policies(self, policies):
        """
        Converts a policy from Boto3 formatting to standard Python/Ansible
        formatting
        """
        normalized = []
        for policy in policies:
            _policy = camel_dict_to_snake_dict(policy)
            if policy.get('Tags'):
                _policy['tags'] = boto3_tag_list_to_ansible_dict(policy.get('Tags'))
            elif policy.get('Tags') is not None:
                _policy['tags'] = {}
            normalized += [_policy]
        return normalized

    def __init__(self, connection, module):
        self.connection = connection
        self.module = module
        self.fetch_targets = module.params.get('fetch_targets', False)

    #  Wrap Paginated queries because retry_decorator doesn't handle pagination
    @AWSRetry.jittered_backoff()
    def _list_policies(self, **params):
        paginator = self.connection.get_paginator('list_policies')
        return paginator.paginate(**params).build_full_result()

    @AWSRetry.jittered_backoff()
    def _list_targets(self, **params):
        paginator = self.connection.get_paginator('list_targets_for_policy')
        return paginator.paginate(**params).build_full_result()

    def update_policy(self, policy, name=None, content=None, description=None):
        try:
            policy_detail = self.connection.describe_policy(aws_retry=True, PolicyId=policy)['Policy']
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
            self.module.fail_json_aws(e, 'Failed to describe policy {0}'.format(policy))

        changes = {}
        if name and name != policy_detail['PolicySummary']['Name']:
            changes['Name'] = name
        if description and description != policy_detail['PolicySummary']['Description']:
            changes['Description'] = description
        if content:
            new_content = json.loads(content)
            old_content = json.loads(policy_detail['Content'])
            if compare_policies(old_content, new_content):
                changes['Content'] = content

        if not changes:
            return False

        if self.module.check_mode:
            return True

        try:
            self.connection.update_policy(aws_retry=True, PolicyId=policy, **changes)
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
            self.module.fail_json_aws(e, 'Failed to update policy')

        return True

    def create_policy(self, name, content, policy_type, description="", tags=None):
        policy_type = self._TYPE_MAPPING.get(policy_type, policy_type)
        if self.module.check_mode:
            return (True, None)
        if tags is None:
            tags = {}
        try:
            created = self.connection.create_policy(
                aws_retry=True,
                Content=content,
                Description=description,
                Name=name,
                Type=policy_type)['Policy']
            # Tagging support added to create 2020-09
            self.connection.tag_resource(
                ResourceId=created['PolicySummary']['Id'],
                Tags=ansible_dict_to_boto3_tag_list(tags))
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
            self.module.fail_json_aws(e, 'Failed to create policy')
        return (True, created['PolicySummary']['Id'])

    def update_policy_tags(self, policy_id, tags=None, purge_tags=True):
        if tags is None:
            return False
        old_tags = self.connection.list_tags_for_resource(aws_retry=True, ResourceId=policy_id)['Tags']
        old_tags = boto3_tag_list_to_ansible_dict(old_tags)

        tags_to_set, tags_to_delete = compare_aws_tags(old_tags, tags, purge_tags=purge_tags)
        # Nothing to change
        if not bool(tags_to_delete or tags_to_set):
            return False

        if self.module.check_mode:
            return True

        if tags_to_set:
            try:
                self.connection.tag_resource(
                    aws_retry=True,
                    ResourceId=policy_id,
                    Tags=ansible_dict_to_boto3_tag_list(tags_to_set))
            except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
                self.module.fail_json_aws(e, 'Failed to set tags')
        if tags_to_delete:
            try:
                self.connection.untag_resource(
                    aws_retry=True,
                    ResourceId=policy_id,
                    TagKeys=tags_to_delete.keys())
            except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
                self.module.fail_json_aws(e, 'Failed to remove tags')

        return True

    def describe_policy(self, policy):
        """
        Describe a named policy
        """
        try:
            description = self.connection.describe_policy(aws_retry=True, PolicyId=policy)['Policy']
        except is_boto3_error_code('PolicyNotFoundException'):
            return None
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:  # pylint: disable=duplicate-except
            self.module.fail_json_aws(e, 'Failed to describe policy {0}'.format(policy))
        try:
            description['Tags'] = self.connection.list_tags_for_resource(aws_retry=True, ResourceId=policy)['Tags']
        except is_boto3_error_code('AccessDeniedException'):
            self.module.warn('Access Denied fetching Tags')
            description['Tags'] = []
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:  # pylint: disable=duplicate-except
            self.module.fail_json_aws(e, 'Failed to describe policy {0}'.format(policy))

        if self.fetch_targets:
            try:
                targets = self._list_targets(PolicyId=policy)
            except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
                self.module.fail_json_aws(e, 'Failed to list targets for policy {0}'.format(policy))
            description.update(targets)
        return description

    def delete_policy(self, policy, force_delete):
        if policy is None:
            return False

        changed = False

        try:
            targets = self._list_targets(PolicyId=policy)['Targets']
        except is_boto3_error_code('AccessDeniedException'):
            self.module.warn('Access Denied fetching policy targets')
            targets = []
        except is_boto3_error_code('PolicyNotFoundException'):  # pylint: disable=duplicate-except
            self.module.warn('Attempted to delete a non-existent policy {0}'.format(policy))
            return False
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:  # pylint: disable=duplicate-except
            self.module.fail_json_aws(e, 'Failed to list targets for policy {0}'.format(policy))

        # Before deleting a policy it must be detatched from all targets
        if targets:
            # This can have a very broad affect, use force_delete as a molly guard.
            if not force_delete:
                targets = [camel_dict_to_snake_dict(target) for target in targets]
                self.module.fail_json('Unable to delete policy {0} - still attached'.format(policy),
                                      targets=targets)
            if self.module.check_mode:
                return True

            for target in targets:
                try:
                    self.connection.detach_policy(aws_retry=True, PolicyId=policy, TargetId=target['TargetId'])
                except is_boto3_error_code('PolicyNotAttachedException'):
                    # Already detached
                    pass
                except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:  # pylint: disable=duplicate-except
                    self.module.fail_json_aws(e, 'Failed to detach policy {0} from target {1}'.format(policy, target['TargetId']))
                changed = True

        try:
            if self.module.check_mode:
                return True
            self.connection.delete_policy(aws_retry=True, PolicyId=policy)
            changed = True
        except is_boto3_error_code('PolicyNotFoundException'):
            # Already deleted
            pass
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:  # pylint: disable=duplicate-except
            self.module.fail_json_aws(e, 'Failed to detach policy {0} from target {1}'.format(policy, target['TargetId']))

        return changed

    def delete_policies(self, policies, force_delete=False):
        if policies is None:
            return False

        changed = False
        for policy in policies:
            changed |= self.delete_policy(policy, force_delete)

        return changed

    def describe_policies(self, policies):
        described_policies = []
        for policy in policies:
            description = self.describe_policy(policy)
            if description:
                described_policies += [description]
        return described_policies

    def _list_policies(self, policy_type):
        """
        Fetches a list of policy IDs of type policy_type.
        """
        policy_type = self._TYPE_MAPPING.get(policy_type, policy_type)

        # Unlike most 'filters' this is just a single string
        try:
            policies = self.connection.list_policies(aws_retry=True, Filter=policy_type)
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
            self.module.fail_json_aws(e, 'Failed to list policies')

        if not policies['Policies']:
            self.module.fail_json('Failed to list policies - Policies missing from returned value.')

        return policies['Policies']

    def list_policies(self, policy_type=None):
        policies = self._list_policies(policy_type=policy_type)
        return [policy.get('Id') for policy in policies]

    def find_policy_by_name(self, name, policy_type):
        """
        Iterates through the list of known policies and returns the ID of the
        policy with name set to name.
        """
        policies = self._list_policies(policy_type=policy_type)
        matching_policies = list(filter(lambda p: p['Name'] == name, policies))
        return [policy.get('Id') for policy in matching_policies]
