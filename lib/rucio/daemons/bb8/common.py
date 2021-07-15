# Copyright 2016-2021 CERN
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Authors:
# - Martin Barisits <martin.barisits@cern.ch>, 2016-2021
# - Vincent Garonne <vincent.garonne@cern.ch>, 2016-2018
# - Tomas Javurek <tomas.javurek@cern.ch>, 2017-2019
# - Cedric Serfon <cedric.serfon@cern.ch>, 2017-2021
# - Dimitrios Christidis <dimitrios.christidis@cern.ch>, 2018-2020
# - Hannes Hansen <hannes.jakob.hansen@cern.ch>, 2018
# - Andrew Lister <andrew.lister@stfc.ac.uk>, 2019
# - Eli Chadwick <eli.chadwick@stfc.ac.uk>, 2020
# - Patrick Austin <patrick.austin@stfc.ac.uk>, 2020
# - Thomas Beermann <thomas.beermann@cern.ch>, 2021

from __future__ import print_function, division

import logging

from datetime import datetime, date, timedelta
from string import Template
from sqlalchemy.orm import aliased
from sqlalchemy import func, and_, or_, cast, BigInteger
from sqlalchemy.sql.expression import case, select

from rucio.core import config as config_core
from rucio.core.lock import get_dataset_locks
from rucio.core.rule import get_rule, add_rule, update_rule
from rucio.core.rse_expression_parser import parse_expression
from rucio.core.rse import list_rse_attributes, get_rse_name, get_rse_vo
from rucio.core.rse_selector import RSESelector
from rucio.common.config import config_get
from rucio.common.exception import (InsufficientTargetRSEs, RuleNotFound, DuplicateRule,
                                    InsufficientAccountLimit)
from rucio.common.types import InternalAccount

from rucio.db.sqla.session import transactional_session, read_session
from rucio.db.sqla import models
from rucio.db.sqla.constants import (DIDType, RuleState, RuleGrouping)
from requests import get


@transactional_session
def rebalance_rule(parent_rule, activity, rse_expression, priority, source_replica_expression='*\\bb8-enabled=false', comment=None, session=None):
    """
    Rebalance a replication rule to a new RSE
    :param parent_rule:                Replication rule to be rebalanced.
    :param activity:                   Activity to be used for the rebalancing.
    :param rse_expression:             RSE expression of the new rule.
    :param priority:                   Priority of the newly created rule.
    :param source_replica_expression:  Source replica expression of the new rule.
    :param comment:                    Comment to set on the new rules.
    :returns:                          The new child rule id.
    """

    if parent_rule['expires_at'] is None:
        lifetime = None
    else:
        lifetime = (parent_rule['expires_at'] - datetime.utcnow()).days * 24 * 3600 + (parent_rule['expires_at'] - datetime.utcnow()).seconds

    if parent_rule['grouping'] == RuleGrouping.ALL:
        grouping = 'ALL'
    elif parent_rule['grouping'] == RuleGrouping.NONE:
        grouping = 'NONE'
    else:
        grouping = 'DATASET'

    # check if concurrent replica at target rse does not exist
    concurrent_replica = False
    for lock in get_dataset_locks(parent_rule['scope'], parent_rule['name']):
        lock_rse_expr = lock['rse']
        if lock_rse_expr == rse_expression:
            concurrent_replica = True

    if concurrent_replica:
        return None

    child_rule = add_rule(dids=[{'scope': parent_rule['scope'],
                                 'name': parent_rule['name']}],
                          account=parent_rule['account'],
                          copies=parent_rule['copies'],
                          rse_expression=rse_expression,
                          grouping=grouping,
                          weight=parent_rule['weight'],
                          lifetime=lifetime,
                          locked=parent_rule['locked'],
                          subscription_id=parent_rule['subscription_id'],
                          source_replica_expression=source_replica_expression,
                          activity=activity,
                          notify=parent_rule['notification'],
                          purge_replicas=parent_rule['purge_replicas'],
                          ignore_availability=False,
                          comment=parent_rule['comments'] if not comment else comment,
                          ask_approval=False,
                          asynchronous=False,
                          ignore_account_limit=True,
                          priority=priority, session=session)[0]

    update_rule(rule_id=parent_rule['id'], options={'child_rule_id': child_rule, 'lifetime': 0}, session=session)
    return child_rule


def __dump_url(rse_id, logger=logging.log):
    """
    getting potential urls of the dump over last week
    :param rse_id:                     RSE where the dump is released.
    :param logger:                     Logger.
    """

    rse = get_rse_name(rse_id=rse_id)
    vo = get_rse_vo(rse_id=rse_id)

    # get the date of the most recent dump
    today = date.today()
    dump_dates = []
    dump_production_day = config_get('bb8', 'dump_production_day', raise_exception=False, default=None)
    if dump_production_day is None:
        for idx in range(0, 7):
            dump_date = today - timedelta(idx)
            dump_dates.append(dump_date.strftime('%d-%m-%Y'))
    else:
        weekdays = {'Sunday': 6, 'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3, 'Friday': 4, 'Saturday': 5}
        if dump_production_day not in weekdays:
            logger(logging.WARNING, 'ERROR: please set the day of a dump creation in bb8 config correctly, e.g. Monday')
            return False
        today_idx = (today.weekday() - weekdays[dump_production_day]) % 7
        dump_date = today - timedelta(today_idx)
        dump_dates = [dump_date.strftime('%d-%m-%Y')]

    # getting structure (template) of url location of a dump
    url_template_str = config_get('bb8', 'dump_url_template', raise_exception=False, default='http://rucio-analytix.cern.ch:8080/LOCKS/GetFileFromHDFS?date=${date}&rse=${rse}')
    url_template = Template(url_template_str)

    # populating url template
    urls = []
    for d in dump_dates:
        url = url_template.substitute({'date': d, 'rse': rse, 'vo': vo})
        urls.append(url)
    return urls


def _list_rebalance_rule_candidates_dump(rse_id, mode=None, logger=logging.log):
    """
    Download dump to temporary directory
    :param rse_id:                     RSE of the source.
    :param mode:                       Rebalancing mode.
    :param logger:                     Logger.
    """

    # fetching the dump
    candidates = []
    rules = {}
    rse_dump_urls = __dump_url(rse_id=rse_id)
    rse_dump_urls.reverse()
    resp = None
    if not rse_dump_urls:
        logger(logging.DEBUG, 'URL of the dump was not built from template.')
        return candidates
    success = False
    while not success and len(rse_dump_urls):
        url = rse_dump_urls.pop()
        resp = get(url, stream=True)
        if resp:
            success = True
    if not resp or resp is None:
        logger(logging.WARNING, 'RSE dump not available')
        return candidates

    # looping over the dump and selecting the rules
    for line in resp.iter_lines():
        if line:
            file_scope, file_name, rule_id, rse_expression, account, file_size, state = line.split('\t')
            if rule_id not in rules:
                rule_info = {}
                try:
                    rule_info = get_rule(rule_id=rule_id)
                except Exception as err:
                    rules[rule_id] = {'state': 'DELETED'}
                    logger(logging.ERROR, str(err))
                    continue
                if rule_info['child_rule_id']:
                    rules[rule_id] = {'state': 'DELETED'}
                    continue
                rules[rule_id] = {'scope': rule_info['scope'],
                                  'name': rule_info['name'],
                                  'rse_expression': rse_expression,
                                  'subscription_id': rule_info['subscription_id'],
                                  'length': 1,
                                  'state': 'ACTIVE',
                                  'bytes': int(file_size)}
            elif rules[rule_id]['state'] == 'ACTIVE':
                rules[rule_id]['length'] += 1
                rules[rule_id]['bytes'] += int(file_size)

    # looping over agragated rules collected from dump
    for r_id in rules:
        if mode == 'decommission':  # other modes can be added later
            if rules[r_id]['state'] == 'DELETED':
                continue
            if int(rules[r_id]['length']) == 0:
                continue
            candidates.append((rules[r_id]['scope'],
                               rules[r_id]['name'],
                               r_id,
                               rules[r_id]['rse_expression'],
                               rules[r_id]['subscription_id'],
                               rules[r_id]['bytes'],
                               rules[r_id]['length'],
                               int(rules[r_id]['bytes'] / rules[r_id]['length'])))
    return candidates


@transactional_session
def list_rebalance_rule_candidates(rse_id, mode=None, session=None):
    """
    List the rebalance rule candidates based on the agreed on specification
    :param rse_id:       RSE of the source.
    :param mode:         Rebalancing mode.
    :param session:      DB Session.
    """

    vo = get_rse_vo(rse_id=rse_id)

    # dumps can be applied only for decommission since the dumps doesn't contain info from dids
    if mode == 'decommission':
        return _list_rebalance_rule_candidates_dump(rse_id, mode)

    # the rest is done with sql query
    from_date = datetime.utcnow() + timedelta(days=60)
    to_date = datetime.now() - timedelta(days=60)
    to_date = datetime.now() + timedelta(days=60)
    allowed_accounts = config_core.get(section='bb8', option='allowed_accounts', default='panda,root,ddmadmin,jdoe', use_cache=True, expiration_time=3600, session=session)
    allowed_accounts = [InternalAccount(acc.strip(' '), vo=vo) for acc in allowed_accounts.split(',')]
    allowed_grouping = [RuleGrouping.DATASET, RuleGrouping.ALL]
    external_dsl = aliased(models.DatasetLock)
    count_locks = select([func.count()]).where(and_(external_dsl.scope == models.DatasetLock.scope,
                                                    external_dsl.name == models.DatasetLock.name,
                                                    external_dsl.rse_id == models.DatasetLock.rse_id)).as_scalar()
    query = session.query(models.DatasetLock.scope,
                          models.DatasetLock.name,
                          models.ReplicationRule.id,
                          models.ReplicationRule.rse_expression,
                          models.ReplicationRule.subscription_id,
                          models.DataIdentifier.bytes,
                          models.DataIdentifier.length,
                          case([(or_(models.DatasetLock.length < 1, models.DatasetLock.length.is_(None)), 0)],
                               else_=cast(models.DatasetLock.bytes / models.DatasetLock.length, BigInteger))).\
        join(models.ReplicationRule, models.ReplicationRule.id == models.DatasetLock.rule_id).\
        join(models.DataIdentifier, and_(models.DatasetLock.scope == models.DataIdentifier.scope, models.DatasetLock.name == models.DataIdentifier.name)).\
        filter(models.DatasetLock.rse_id == rse_id).\
        filter(or_(models.ReplicationRule.expires_at > from_date, models.ReplicationRule.expires_at.is_(None))).\
        filter(and_(models.ReplicationRule.created_at < to_date,
                    models.ReplicationRule.account.in_(allowed_accounts),
                    models.ReplicationRule.state == RuleState.OK,
                    models.ReplicationRule.did_type == DIDType.DATASET,
                    models.ReplicationRule.copies == 1,
                    models.ReplicationRule.child_rule_id.is_(None),
                    models.ReplicationRule.grouping.in_(allowed_grouping))).\
        filter(and_(models.DataIdentifier.bytes.isnot(None),
                    models.DataIdentifier.is_open == False, # NOQA
                    models.DataIdentifier.did_type == DIDType.DATASET)).\
        filter(case([(or_(models.DatasetLock.length < 1, models.DatasetLock.length.is_(None)), 0)],
                    else_=cast(models.DatasetLock.bytes / models.DatasetLock.length, BigInteger)) > 1000000000).\
        filter(count_locks == 1)
    summary = query.order_by(case([(or_(models.DatasetLock.length < 1, models.DatasetLock.length.is_(None)), 0)],
                                  else_=cast(models.DatasetLock.bytes / models.DatasetLock.length, BigInteger)),
                             models.DatasetLock.accessed_at).all()
    return summary


@read_session
def select_target_rse(parent_rule, current_rse_id, rse_expression, subscription_id, rse_attributes, other_rses=[], exclude_expression=None, force_expression=None, session=None):
    """
    Select a new target RSE for a rebalanced rule.
    :param parent_rule           rule that is rebalanced.
    :param current_rse_id:       RSE of the source.
    :param rse_expression:       RSE Expression of the source rule.
    :param subscription_id:      Subscription ID of the source rule.
    :param rse_attributes:       The attributes of the source rse.
    :param other_rses:           Other RSEs with existing dataset replicas.
    :param exclude_expression:   Exclude this rse_expression from being target_rses.
    :param force_expression:     Force a specific rse_expression as target.
    :param session:              The DB Session.
    :returns:                    New RSE expression.
    """

    current_rse = get_rse_name(rse_id=current_rse_id)
    current_rse_expr = current_rse
    # if parent rule has a vo, enforce it
    vo = parent_rule['scope'].vo
    if exclude_expression:
        target_rse = '((%s)|(%s))' % (exclude_expression, current_rse_expr)
    else:
        target_rse = current_rse_expr
    list_target_rses = [rse['rse'] for rse in parse_expression(expression=target_rse, filter_={'vo': vo}, session=session)]
    list_target_rses.sort()

    rses = parse_expression(expression=rse_expression, filter_={'vo': vo}, session=session)

    # TODO: Implement subscription rebalancing

    if force_expression is not None:
        if parent_rule['grouping'] != RuleGrouping.NONE:
            rses = parse_expression(expression='(%s)\\%s' % (force_expression, target_rse), filter_={'vo': vo, 'availability_write': True}, session=session)
        else:
            # in order to avoid replication of the part of distributed dataset not present at rebalanced rse -> rses in force_expression
            # this will be extended with development of delayed rule
            rses = parse_expression(expression='((%s)|(%s))\\%s' % (force_expression, rse_expression, target_rse), filter_={'vo': vo, 'availability_write': True}, session=session)
    else:
        # force_expression is not set, RSEs will be selected as rse_expression\rse
        list_rses = [rse['rse'] for rse in rses]
        list_rses.sort()
        if list_rses == list_target_rses:
            raise InsufficientTargetRSEs('Not enough RSEs to rebalance rule %s' % parent_rule['id'])
        else:
            rses = parse_expression(expression='(%s)\\%s' % (rse_expression, target_rse), filter_={'vo': vo, 'availability_write': True}, session=session)
    rseselector = RSESelector(account=InternalAccount('root', vo=vo), rses=rses, weight='freespace', copies=1, ignore_account_limit=True, session=session)
    return get_rse_name([rse_id for rse_id, _, _ in rseselector.select_rse(size=0, preferred_rse_ids=[], blocklist=other_rses)][0], session=session)


@transactional_session
def rebalance_rse(rse_id, max_bytes=1E9, max_files=None, dry_run=False, exclude_expression=None, comment=None, force_expression=None, mode=None, priority=3, source_replica_expression='*\\bb8-enabled=false', session=None, logger=logging.log):
    """
    Rebalance data from an RSE
    :param rse_id:                     RSE to rebalance data from.
    :param max_bytes:                  Maximum amount of bytes to rebalance.
    :param max_files:                  Maximum amount of files to rebalance.
    :param dry_run:                    Only run in dry-run mode.
    :param exclude_expression:         Exclude this rse_expression from being target_rses.
    :param comment:                    Comment to set on the new rules.
    :param force_expression:           Force a specific rse_expression as target.
    :param mode:                       BB8 mode to execute (None=normal, 'decomission'=Decomission mode)
    :param priority:                   Priority of the new created rules.
    :param source_replica_expression:  Source replica expression of the new created rules.
    :param session:                    The database session.
    :param logger:                     Logger.
    :returns:                          List of rebalanced datasets.
    """
    rebalanced_bytes = 0
    rebalanced_files = 0
    rebalanced_datasets = []

    rse_attributes = list_rse_attributes(rse_id=rse_id, session=session)
    src_rse = get_rse_name(rse_id=rse_id)

    logger(logging.INFO, '***************************')
    logger(logging.INFO, 'BB8 - Execution Summary')
    logger(logging.INFO, 'Mode:    %s' % ('STANDARD' if mode is None else mode.upper()))
    logger(logging.INFO, 'Dry Run: %s' % (dry_run))
    logger(logging.INFO, '***************************')

    for scope, name, rule_id, rse_expression, subscription_id, bytes_, length, fsize in list_rebalance_rule_candidates(rse_id=rse_id, mode=mode):
        if force_expression is not None and subscription_id is not None:
            continue

        if rebalanced_bytes + bytes_ > max_bytes:
            continue
        if max_files:
            if rebalanced_files + length > max_files:
                continue

        try:
            rule = get_rule(rule_id=rule_id)
            other_rses = [r['rse_id'] for r in get_dataset_locks(scope, name, session=session)]
            # Select the target RSE for this rule
            try:
                target_rse_exp = select_target_rse(parent_rule=rule,
                                                   current_rse_id=rse_id,
                                                   rse_expression=rse_expression,
                                                   subscription_id=subscription_id,
                                                   rse_attributes=rse_attributes,
                                                   other_rses=other_rses,
                                                   exclude_expression=exclude_expression,
                                                   force_expression=force_expression,
                                                   session=session)
                # Rebalance this rule
                if not dry_run:
                    child_rule_id = rebalance_rule(parent_rule=rule,
                                                   activity='Data rebalancing',
                                                   rse_expression=target_rse_exp,
                                                   priority=priority,
                                                   source_replica_expression=source_replica_expression,
                                                   comment=comment)
                else:
                    child_rule_id = ''
            except (InsufficientTargetRSEs, DuplicateRule, RuleNotFound, InsufficientAccountLimit) as err:
                logger(logging.ERROR, str(err))
                continue
            if child_rule_id is None:
                logger(logging.WARNING, 'A rule for %s:%s already exists on %s. It cannot be rebalanced', scope, name, target_rse_exp)
                continue
            logger(logging.INFO, 'Rebalancing %s:%s rule %s (%f GB) from %s to %s. New rule %s', scope, name, str(rule_id), bytes_ / 1E9, rule['rse_expression'], target_rse_exp, child_rule_id)
            rebalanced_bytes += bytes_
            rebalanced_files += length
            rebalanced_datasets.append((scope, name, bytes_, length, target_rse_exp, rule_id, child_rule_id))
        except Exception as error:
            logger(logging.ERROR, 'Exception %s occured while rebalancing %s:%s, rule_id: %s!', str(error), scope, name, str(rule_id))

    logger(logging.INFO, 'BB8 is rebalancing %d GB of data (%d rules) from %s', rebalanced_bytes / 1E9, len(rebalanced_datasets), src_rse)
    return rebalanced_datasets
