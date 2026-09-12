// infra/flex.bicep
// Flex Consumption infra for the azure-functions 2.x real-Azure certification
// lane (#350). Creates: Storage Account + Flex Consumption plan + Function App
// (Flex Consumption / Linux / Python 3.13+), which is the hosting model Azure
// requires for Python 3.13/3.14 — Linux Consumption (Y1, see infra/main.bicep)
// caps at Python 3.12 and therefore cannot host the azure-functions 2.x worker.
//
// STATUS: unverified scaffold. This template has NOT yet been deployed to real
// Azure; it exists so the 2.x certification job in e2e-azure.yml has a concrete
// target the first time it is dispatched. Treat the first real dispatch as the
// validation of this file, and do NOT raise the pyproject `azure-functions`
// `<2.0.0` cap until that dispatch is green (#350 acceptance).
//
// The certified example (examples/e2e_app) exercises the native LangGraph
// routes (/api/health, /api/graphs/<name>/invoke, /api/graphs/<name>/stream),
// which need only AzureWebJobsStorage. Platform-compatible routes are out of
// scope for this baseline 2.x runtime certification, mirroring infra/main.bicep.
//
// Usage:
//   az deployment group create -g <rg> -f infra/flex.bicep \
//     -p functionAppName=<name> storageName=<name> location=<loc> \
//        pythonVersion=3.13

@description('Azure region for all resources. Must be a region that offers Flex Consumption.')
param location string = resourceGroup().location

@description('Name of the Function App (must be globally unique).')
param functionAppName string

@description('Name of the Storage Account (3-24 lowercase alphanumeric).')
param storageName string

@description('Python major.minor version for the Flex Consumption runtime (3.13 or 3.14).')
@allowed([
  '3.13'
  '3.14'
])
param pythonVersion string = '3.13'

@description('Blob container used by Flex Consumption for the one-deploy deployment package.')
param deploymentContainerName string = 'deploymentpackage'

@description('Instance memory (MB) for the Flex Consumption plan.')
@allowed([
  512
  2048
  4096
])
param instanceMemoryMB int = 2048

@description('Maximum instance count for the Flex Consumption plan.')
param maximumInstanceCount int = 40

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

// ── Deployment container (Flex Consumption one-deploy package store) ────────
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: deploymentContainerName
}

// ── Flex Consumption Hosting Plan ──────────────────────────────────────────
resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${functionAppName}-flexplan'
  location: location
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  kind: 'functionapp'
  properties: {
    reserved: true
  }
}

// ── Function App (Flex Consumption) ────────────────────────────────────────
// Flex Consumption configures the runtime through `functionAppConfig`
// (deployment + runtime + scaleAndConcurrency) rather than
// `siteConfig.linuxFxVersion` used by the Y1 Linux Consumption template. The
// worker runtime is declared here, so FUNCTIONS_WORKER_RUNTIME is intentionally
// NOT set as an app setting.
resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  properties: {
    serverFarmId: hostingPlan.id
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storageAccount.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'StorageAccountConnectionStringSecret'
            storageAccountConnectionStringName: 'DEPLOYMENT_STORAGE_CONNECTION_STRING'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
      runtime: {
        name: 'python'
        version: pythonVersion
      }
    }
    siteConfig: {
      appSettings: [
        { name: 'AzureWebJobsStorage', value: storageConnectionString }
        { name: 'DEPLOYMENT_STORAGE_CONNECTION_STRING', value: storageConnectionString }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
      ]
    }
    httpsOnly: true
  }
}

// ── Outputs ────────────────────────────────────────────────────────────────
output functionAppName string = functionApp.name
output defaultHostName string = functionApp.properties.defaultHostName
