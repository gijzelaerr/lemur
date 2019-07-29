import time
import requests
import json
from .ultradns_zone import Zone
from .ultradns_record import Record

import dns
import dns.exception
import dns.name
import dns.query
import dns.resolver

from flask import current_app
from lemur.extensions import metrics, sentry


def get_ultradns_token():
    # Function to call the UltraDNS Authorization API. Returns the Authorization access_token
    # which is valid for 1 hour. Each request calls this function and we generate a new token
    # every time.
    path = "/v2/authorization/token"
    data = {
        "grant_type": "password",
        "username": current_app.config.get("ACME_ULTRADNS_USERNAME", ""),
        "password": current_app.config.get("ACME_ULTRADNS_PASSWORD", ""),
    }
    base_uri = current_app.config.get("ACME_ULTRADNS_DOMAIN", "")
    resp = requests.post("{0}{1}".format(base_uri, path), data=data, verify=True)
    return resp.json()["access_token"]


def _generate_header():
    # Function to generate the header for a request. Contains the Authorization access_key
    # obtained from the get_ultradns_token() function.
    access_token = get_ultradns_token()
    return {"Authorization": "Bearer {}".format(access_token), "Content-Type": "application/json"}


def _paginate(path, key):
    limit = 100
    params = {"offset": 0, "limit": 1}
    resp = _get(path, params)
    for index in range(0, resp["resultInfo"]["totalCount"], limit):
        params["offset"] = index
        params["limit"] = limit
        resp = _get(path, params)
        yield resp[key]


def _get(path, params=None):
    # Function to execute a GET request on the given URL (base_uri + path) with given params
    base_uri = current_app.config.get("ACME_ULTRADNS_DOMAIN", "")
    resp = requests.get(
        "{0}{1}".format(base_uri, path),
        headers=_generate_header(),
        params=params,
        verify=True,
    )
    resp.raise_for_status()
    return resp.json()


def _delete(path):
    # Function to execute a DELETE request on the given URL
    base_uri = current_app.config.get("ACME_ULTRADNS_DOMAIN", "")
    resp = requests.delete(
        "{0}{1}".format(base_uri, path),
        headers=_generate_header(),
        verify=True,
    )
    resp.raise_for_status()


def _post(path, params):
    # Executes a POST request on given URL. Body is sent in JSON format
    base_uri = current_app.config.get("ACME_ULTRADNS_DOMAIN", "")
    resp = requests.post(
        "{0}{1}".format(base_uri, path),
        headers=_generate_header(),
        data=json.dumps(params),
        verify=True,
    )
    resp.raise_for_status()


def _has_dns_propagated(name, token, domain="8.8.8.8"):
    # Check whether the DNS change made by Lemur have propagated to the public DNS or not.
    # Invoked by wait_for_dns_change() function
    txt_records = []
    try:
        dns_resolver = dns.resolver.Resolver()
        # dns_resolver.nameservers = [get_authoritative_nameserver(name)]
        # dns_resolver.nameservers = ["156.154.64.154"]
        dns_resolver.nameservers = [domain]
        dns_response = dns_resolver.query(name, "TXT")
        for rdata in dns_response:
            for txt_record in rdata.strings:
                txt_records.append(txt_record.decode("utf-8"))
    except dns.exception.DNSException:
        metrics.send("has_dns_propagated_fail", "counter", 1)
        return False

    for txt_record in txt_records:
        if txt_record == token:
            metrics.send("has_dns_propagated_success", "counter", 1)
            return True

    return False


def wait_for_dns_change(change_id, account_number=None):
    # Waits and checks if the DNS changes have propagated or not.
    fqdn, token = change_id
    number_of_attempts = 20
    for attempts in range(0, number_of_attempts):
        status = _has_dns_propagated(fqdn, token, "156.154.64.154")
        current_app.logger.debug("Record status for fqdn: {}: {}".format(fqdn, status))
        if status:
            # metrics.send("wait_for_dns_change_success", "counter", 1)
            time.sleep(10)
            break
        time.sleep(10)
    if status:
        for attempts in range(0, number_of_attempts):
            status = _has_dns_propagated(fqdn, token, "8.8.8.8")
            current_app.logger.debug("Record status for fqdn: {}: {}".format(fqdn, status))
            if status:
                metrics.send("wait_for_dns_change_success", "counter", 1)
                break
            time.sleep(10)
    if not status:
        # TODO: Delete associated DNS text record here
        metrics.send("wait_for_dns_change_fail", "counter", 1)
        sentry.captureException(extra={"fqdn": str(fqdn), "txt_record": str(token)})
        metrics.send(
            "wait_for_dns_change_error",
            "counter",
            1,
            metric_tags={"fqdn": fqdn, "txt_record": token},
        )
    return


def get_zones(account_number):
    # Get zones from the UltraDNS
    path = "/v2/zones"
    zones = []
    for page in _paginate(path, "zones"):
        for elem in page:
            # UltraDNS zone names end with a "." - Example - lemur.example.com.
            # We pick out the names minus the "." at the end while returning the list
            zone = Zone(elem)
            # TODO : Check for active & Primary
            # if elem["properties"]["type"] == "PRIMARY" and elem["properties"]["status"] == "ACTIVE":
            if zone.authoritative_type == "PRIMARY" and zone.status == "ACTIVE":
                zones.append(zone.name)

    return zones


def get_zone_name(domain, account_number):
    # Get the matching zone for the given domain
    zones = get_zones(account_number)
    zone_name = ""
    for z in zones:
        if domain.endswith(z):
            # Find the most specific zone possible for the domain
            # Ex: If fqdn is a.b.c.com, there is a zone for c.com,
            # and a zone for b.c.com, we want to use b.c.com.
            if z.count(".") > zone_name.count("."):
                zone_name = z
    if not zone_name:
        metrics.send("ultradns_no_zone_name", "counter", 1)
        raise Exception("No UltraDNS zone found for domain: {}".format(domain))
    return zone_name


def create_txt_record(domain, token, account_number):
    # Create a TXT record for the given domain.
    # The part of the domain that matches with the zone becomes the zone name.
    # The remainder becomes the owner name (referred to as node name here)
    # Example: Let's say we have a zone named "exmaple.com" in UltraDNS and we
    # get a request to create a cert for lemur.example.com
    # Domain - _acme-challenge.lemur.example.com
    # Matching zone - example.com
    # Owner name - _acme-challenge.lemur

    zone_name = get_zone_name(domain, account_number)
    zone_parts = len(zone_name.split("."))
    node_name = ".".join(domain.split(".")[:-zone_parts])
    fqdn = "{0}.{1}".format(node_name, zone_name)
    path = "/v2/zones/{0}/rrsets/TXT/{1}".format(zone_name, node_name)
    params = {
        "ttl": 5,
        "rdata": [
            "{}".format(token)
        ],
    }

    try:
        _post(path, params)
        current_app.logger.debug(
            "TXT record created: {0}, token: {1}".format(fqdn, token)
        )
    except Exception as e:
        current_app.logger.debug(
            "Unable to add record. Domain: {}. Token: {}. "
            "Record already exists: {}".format(domain, token, e),
            exc_info=True,
        )

    change_id = (fqdn, token)
    return change_id


def delete_txt_record(change_id, account_number, domain, token):
    # Delete the TXT record that was created in the create_txt_record() function.
    # UltraDNS handles records differently compared to Dyn. It creates an RRSet
    # which is a set of records of the same type and owner. This means
    # that while deleting the record, we cannot delete any individual record from
    # the RRSet. Instead, we have to delete the entire RRSet. If multiple certs are
    # being created for the same domain at the same time, the challenge TXT records
    # that are created will be added under the same RRSet. If the RRSet had more
    # than 1 record, then we create a new RRSet on UltraDNS minus the record that
    # has to be deleted.

    if not domain:
        current_app.logger.debug("delete_txt_record: No domain passed")
        return

    zone_name = get_zone_name(domain, account_number)
    zone_parts = len(zone_name.split("."))
    node_name = ".".join(domain.split(".")[:-zone_parts])
    path = "/v2/zones/{}/rrsets/16/{}".format(zone_name, node_name)

    try:
        rrsets = _get(path)
        record = Record(rrsets)
    except Exception as e:
        metrics.send("delete_txt_record_geterror", "counter", 1)
        # No Text Records remain or host is not in the zone anymore because all records have been deleted.
        return
    try:
        # Remove the record from the RRSet locally
        # rrsets["rrSets"][0]["rdata"].remove("{}".format(token))
        record.rdata.remove("{}".format(token))
    except ValueError:
        current_app.logger.debug("Token not found")
        return

    # Delete the RRSet from UltraDNS
    _delete(path)

    # Check if the RRSet has more records. If yes, add the modified RRSet back to UltraDNS
    # if len(rrsets["rrSets"][0]["rdata"]) > 0:
    if len(record.rdata) > 0:
        params = {
            "ttl": 5,
            "rdata": record.rdata,
        }
        _post(path, params)


def delete_acme_txt_records(domain):

    if not domain:
        current_app.logger.debug("delete_acme_txt_records: No domain passed")
        return
    acme_challenge_string = "_acme-challenge"
    if not domain.startswith(acme_challenge_string):
        current_app.logger.debug(
            "delete_acme_txt_records: Domain {} doesn't start with string {}. "
            "Cowardly refusing to delete TXT records".format(
                domain, acme_challenge_string
            )
        )
        return

    zone_name = get_zone_name(domain)
    zone_parts = len(zone_name.split("."))
    node_name = ".".join(domain.split(".")[:-zone_parts])
    path = "/v2/zones/{}/rrsets/16/{}".format(zone_name, node_name)

    _delete(path)


def get_authoritative_nameserver(domain):
    """
    REMEMBER TO CHANGE THE RETURN VALUE
    REMEMBER TO CHANGE THE RETURN VALUE
    REMEMBER TO CHANGE THE RETURN VALUE
    REMEMBER TO CHANGE THE RETURN VALUE
    REMEMBER TO CHANGE THE RETURN VALUE
    REMEMBER TO CHANGE THE RETURN VALUE
    REMEMBER TO CHANGE THE RETURN VALUE
    REMEMBER TO CHANGE THE RETURN VALUE
    REMEMBER TO CHANGE THE RETURN VALUE
    REMEMBER TO CHANGE THE RETURN VALUE
    REMEMBER TO CHANGE THE RETURN VALUE
    """
    # return "8.8.8.8"
    return "156.154.64.154"
