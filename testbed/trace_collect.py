"""Collect a real DNS trace: poll the A records of popular domains through a public resolver.

    python -m testbed.trace_collect --minutes 150 --interval 30 --out results/traces/real_trace.jsonl

Every line is one observation:  {"t": epoch seconds, "name": ..., "rcode": ..., "ttl": ..., "addrs": [...]}
The TTL is the remaining TTL reported by the resolver's cache (it counts down); the replay code uses the
largest TTL seen for a name as that name's nominal TTL.  Only public A records of well-known domains are
queried, at a rate of a few queries per second.
"""
import argparse
import asyncio
import json
import os
import time

import dns.asyncquery
import dns.exception
import dns.message

DOMAINS = """
google.com youtube.com facebook.com wikipedia.org amazon.com twitter.com instagram.com linkedin.com netflix.com microsoft.com
apple.com yahoo.com reddit.com whatsapp.com bing.com live.com office.com zoom.us github.com stackoverflow.com
cloudflare.com akamai.com fastly.com adobe.com paypal.com ebay.com spotify.com twitch.tv discord.com slack.com
dropbox.com salesforce.com oracle.com ibm.com intel.com nvidia.com amd.com cisco.com vmware.com samsung.com
sony.com nintendo.com steampowered.com epicgames.com roblox.com ea.com blizzard.com pinterest.com tumblr.com medium.com
quora.com imdb.com bbc.co.uk cnn.com nytimes.com theguardian.com washingtonpost.com forbes.com bloomberg.com reuters.com
espncricinfo.com timesofindia.indiatimes.com hindustantimes.com ndtv.com thehindu.com flipkart.com myntra.com swiggy.com zomato.com paytm.com
irctc.co.in sbi.co.in hdfcbank.com icicibank.com gov.in india.gov.in vit.ac.in iitb.ac.in iitd.ac.in iisc.ac.in
mit.edu stanford.edu harvard.edu berkeley.edu ox.ac.uk cam.ac.uk ietf.org rfc-editor.org icann.org iana.org
python.org nodejs.org rust-lang.org golang.org kernel.org debian.org ubuntu.com archlinux.org fedoraproject.org mozilla.org
apache.org nginx.org postgresql.org mysql.com mongodb.com redis.io docker.com kubernetes.io grafana.com elastic.co
openai.com anthropic.com huggingface.co pytorch.org tensorflow.org kaggle.com coursera.org udemy.com edx.org khanacademy.org
archive.org wordpress.com blogger.com wix.com shopify.com squarespace.com godaddy.com namecheap.com letsencrypt.org gnu.org
""".split()


async def poll_once(sem, resolver, name, out, timeout):
    async with sem:
        t = time.time()
        q = dns.message.make_query(name, "A")
        try:
            r = await dns.asyncquery.udp(q, resolver, timeout=timeout)
            addrs = sorted(rd.to_text() for rs in r.answer if rs.rdtype == 1 for rd in rs)
            ttl = min((rs.ttl for rs in r.answer if rs.rdtype == 1), default=0)
            rec = {"t": round(t, 2), "name": name, "rcode": r.rcode(), "ttl": ttl, "addrs": addrs}
        except (dns.exception.DNSException, OSError):
            rec = {"t": round(t, 2), "name": name, "rcode": -1, "ttl": 0, "addrs": []}
        out.write(json.dumps(rec) + "\n")


async def main(a):
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    names = list(dict.fromkeys(DOMAINS))
    sem = asyncio.Semaphore(20)
    end = time.time() + a.minutes * 60
    n = 0
    with open(a.out, "a", encoding="utf8", buffering=1) as out:
        while time.time() < end:
            t0 = time.time()
            await asyncio.gather(*[poll_once(sem, a.resolver, d, out, 3.0) for d in names])
            n += 1
            if n % 10 == 1:
                print(f"cycle {n}, {len(names)} names, {(end - time.time()) / 60:.0f} min left", flush=True)
            await asyncio.sleep(max(0, a.interval - (time.time() - t0)))
    print("done", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=150)
    ap.add_argument("--interval", type=float, default=30)
    ap.add_argument("--resolver", default="1.1.1.1")
    ap.add_argument("--out", default="results/traces/real_trace.jsonl")
    asyncio.run(main(ap.parse_args()))
