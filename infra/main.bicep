// infra/main.bicep
// Minimal Azure resources for real-Azure e2e certification.
// Creates: Storage Account + Function App (Consumption / Linux / Python 3.10).
//
// The certified example (examples/e2e_app) exercises the native LangGraph
// routes (/api/health, /api/graphs/<name>/invoke, /api/graphs/<name>/stream),
// which need only AzureWebJobsStorage. Platform-compatible routes
// (/api/threads, /api/runs) would additionally require a Blob container and a
// Table; they are intentionally out of scope for this baseline certification.
//
// Usage:
//   az deployment group create -g <rg> -f infra/main.bicep \
//     -p functionAppName=<name> storageName=<name> location=<loc>

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Name of the Function App (must be globally unique). This is App A.')
param functionAppName string

@description('Name of the second Function App for the two-app single-writer e2e (#386). This is App B. Empty string skips App B.')
param functionAppNameB string = ''

@description('Name of the Storage Account (3-24 lowercase alphanumeric). Shared by App A and App B.')
param storageName string

@description('Blob container for AzureBlobLeaseThreadLock leases (shared by both apps).')
param lockContainerName string = 'langgraph-locks'

@description('Blob container for AzureBlobCheckpointSaver checkpoints (shared by both apps).')
param checkpointContainerName string = 'langgraph-checkpoints'

@description('Enable Application Insights (set true for logging e2e).')
param enableAppInsights bool = false

@description('Name of the Application Insights instance (used when enableAppInsights=true).')
param appInsightsName string = '${functionAppName}-ai'

// ── Storage Account ────────────────────────────────────────────────────────
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storageAccount.listKeys().keys[0].value}'

// ── Blob containers (pre-created to avoid cold-start create races) ──────────
// Both Function Apps share these two containers: one for AzureBlobLeaseThreadLock
// leases, one for AzureBlobCheckpointSaver checkpoints. Pre-creating them here
// makes deployment deterministic instead of relying on runtime create_container().
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource lockContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: lockContainerName
}

resource checkpointContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: checkpointContainerName
}

// Shared app settings for App A and App B. Identical container names on both
// hosts are what make the two apps genuinely contend for one lease / one
// checkpoint namespace.
var baseAppSettings = concat(
  [
    { name: 'AzureWebJobsStorage', value: storageConnectionString }
    { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
    { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
    { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
    { name: 'E2E_LOCK_CONTAINER', value: lockContainerName }
    { name: 'E2E_CHECKPOINT_CONTAINER', value: checkpointContainerName }
  ],
  enableAppInsights
    ? [
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
      ]
    : []
)

// ── App Insights (optional) ────────────────────────────────────────────────
resource appInsights 'Microsoft.Insights/components@2020-02-02' = if (enableAppInsights) {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    RetentionInDays: 30
  }
}

// ── Consumption Hosting Plan ───────────────────────────────────────────────
resource hostingPlan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: '${functionAppName}-plan'
  location: location
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  kind: 'linux'
  properties: {
    reserved: true
  }
}

// ── Function App ───────────────────────────────────────────────────────────
resource functionApp 'Microsoft.Web/sites@2023-01-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  properties: {
    serverFarmId: hostingPlan.id
    siteConfig: {
      linuxFxVersion: 'Python|3.10'
      appSettings: baseAppSettings
    }
    httpsOnly: true
  }
}

// ── App B (two-app single-writer exclusivity, #386) ────────────────────────
// A second, fully independent Function App on its own Consumption plan, sharing
// the SAME storage account, lock container, and checkpoint container as App A.
// Created only when functionAppNameB is provided.
resource hostingPlanB 'Microsoft.Web/serverfarms@2023-01-01' = if (functionAppNameB != '') {
  name: '${functionAppNameB}-plan'
  location: location
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  kind: 'linux'
  properties: {
    reserved: true
  }
}

resource functionAppB 'Microsoft.Web/sites@2023-01-01' = if (functionAppNameB != '') {
  name: functionAppNameB
  location: location
  kind: 'functionapp,linux'
  properties: {
    serverFarmId: hostingPlanB.id
    siteConfig: {
      linuxFxVersion: 'Python|3.10'
      appSettings: baseAppSettings
    }
    httpsOnly: true
  }
}

// ── Outputs ────────────────────────────────────────────────────────────────
output functionAppName string = functionApp.name
output defaultHostName string = functionApp.properties.defaultHostName
output functionAppNameB string = functionAppNameB
output defaultHostNameB string = functionAppNameB != ''
  ? functionAppB.properties.defaultHostName
  : ''
output appInsightsConnectionString string = enableAppInsights
  ? appInsights.properties.ConnectionString
  : ''
