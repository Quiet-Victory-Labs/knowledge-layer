# Publish the project website

The public website is [knowledge-layer.quietvictorylabs.com](https://knowledge-layer.quietvictorylabs.com/).
Its source is the static [`site/`](../site/) directory: HTML, styles, browser
JavaScript, images, sharing metadata, a sitemap and root-level robots directives.
QVL hosts it in the dedicated Vercel project `knowledge-layer-marketing`, under
the `quiet-victory-labs` team.

Deployment input is only `site/`. Application runtime, registry state, customer
sources and credentials stay outside that directory. Self-hosted application
installation follows the [self-hosting guide](self-host.md).

## Link and confirm the project

Use an already authenticated Vercel CLI. From the repository root, explicitly
link the static directory to the dedicated project:

```sh
vercel link --scope quiet-victory-labs --project knowledge-layer-marketing --cwd site
cd site
vercel project inspect --scope quiet-victory-labs --non-interactive
```

Confirm the result names `knowledge-layer-marketing` in the QVL team before
deploying. The directory's `.vercel/` link is local operator state, outside Git.
Keep the committed `vercel.json` static configuration with the site source.

## Stage, verify and promote

Create a production-target deployment without moving the configured domains:

```sh
vercel deploy --prod --skip-domain --scope quiet-victory-labs --non-interactive
```

Replace `DEPLOYMENT_URL` below with the exact deployment URL returned by that
command. Inspect its status and use authenticated requests when deployment
protection is enabled:

```sh
vercel inspect DEPLOYMENT_URL --scope quiet-victory-labs --non-interactive
vercel curl / --deployment DEPLOYMENT_URL --scope quiet-victory-labs
```

Verify desktop and mobile rendering, navigation, install-command copying,
images and sharing metadata. Canonical, Open Graph, structured-data, sitemap
and robots URLs must use `https://knowledge-layer.quietvictorylabs.com/`, with
sharing images under `/assets/`. Check `/robots.txt`, `/sitemap.xml` and the
referenced assets on the staged deployment.

Once the exact staged deployment passes review, assign the project's production
domains without rebuilding:

```sh
vercel promote DEPLOYMENT_URL --scope quiet-victory-labs --non-interactive
```

Verify the canonical HTTPS address serves that deployment and recheck its assets
and navigation. Preserve protected-branch checks and required pull-request review;
a successful deployment does not record merge approval or qualify the
application for customer production use.
