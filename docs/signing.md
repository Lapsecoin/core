# Code signing the Windows build

Unsigned Windows programs start with no reputation, which is the main reason
SmartScreen and antivirus engines are cautious about them. Signing the
executables and the installer is the one thing that changes that.
[SignPath Foundation](https://signpath.org/) signs open source projects for
free, so this repository is set up to use it. Nothing here costs money.

The release workflow signs when the repository variable
`SIGNPATH_ORGANIZATION_ID` exists, and ships unsigned builds until then, so
setting this up can happen at any time.

## One-time setup

1. Apply at https://signpath.org/ (Foundation, open source). The project is
   MIT licensed and public, and the release builds run on GitHub-hosted
   runners, which are the conditions they ask for.
2. In SignPath, create the project with slug `lapsecoin` and a signing policy
   with slug `release-signing`.
3. Add two artifact configurations, slugs `exes` and `msi`, with the XML below.
4. In GitHub, under Settings, Secrets and variables, Actions:
   - variable `SIGNPATH_ORGANIZATION_ID`: your SignPath organization id
   - secret `SIGNPATH_API_TOKEN`: an API token for a SignPath CI user
5. Install the SignPath GitHub App on the repository, as their instructions say.

The slugs and the file names in the XML are what `.github/workflows/release.yml`
refers to. SignPath's configuration format is theirs; check these against
their documentation when setting up, since a mismatch shows up as a failed
signing step, not as a broken release.

### Artifact configuration `exes`

```xml
<artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
  <zip-file>
    <pe-file path="lapsecoin-app.exe">
      <authenticode-sign />
    </pe-file>
    <pe-file path="lapsecoin-bridge.exe">
      <authenticode-sign />
    </pe-file>
    <pe-file path="lapsecoin-dumb.exe">
      <authenticode-sign />
    </pe-file>
  </zip-file>
</artifact-configuration>
```

### Artifact configuration `msi`

```xml
<artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
  <zip-file>
    <msi-file path="lapsecoin.msi">
      <authenticode-sign />
    </msi-file>
  </zip-file>
</artifact-configuration>
```

## What else lowers the flags without signing

Already in place: no UPX, a bootloader compiled from source, a version
resource with company and description, a folder install instead of a
self-unpacking single file. After the first signed release, SmartScreen
reputation still builds up with downloads, but a signed publisher starts far
ahead of an unsigned one.
