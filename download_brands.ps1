$APIFY_TOKEN = "apify_api_7KyC4FzuvRkQmwZD6CWe8OUmY5MDiI22Ypx9"

$brands = @(
    "Essence", "Catrice", "Eveline Cosmetics", "Vivienne Sabo", "Bourjois", 
    "Divage", "Stellary", "Mixit", "Art-Visage", "Natura Siberica", 
    "Avon", "Faberlic", "L'Oreal Paris", "Garnier", "Maybelline", 
    "Max Factor", "Goldwell", "Belor design", "Influence Beauty", "Estrade", 
    "SHU", "Promakeup Laboratory", "Elian Russia", "Ruta", "Russian Beauty Guru", 
    "OK Beauty", "Art&Fact", "Aravia", "Organic Kitchen", "Dolce Milk", 
    "Green Mama", "Ecolatier", "Compliment", "Parisa Cosmetics", "Relouis", 
    "Luxvisage", "Belor Design", "Bielita-Vitex"
)

$allResults = @()

foreach ($brand in $brands) {
    Write-Host "Downloading: $brand ..."
    
    $body = @{
        searchQuery = $brand
        maxItems = 15
    } | ConvertTo-Json

    try {
        $response = Invoke-RestMethod -Uri "https://api.apify.com/v2/acts/parseforge~openbeautyfacts-products-scraper/run-sync-get-dataset-items?token=$APIFY_TOKEN" `
            -Method Post `
            -ContentType "application/json" `
            -Body $body `
            -TimeoutSec 300

        if ($response) {
            $allResults += $response
            Write-Host "  Success. Found: $($response.Count)"
        } else {
            Write-Host "  Not found."
        }
    } catch {
        Write-Host "  Error for $($brand): $_"
    }
    
    Start-Sleep -Seconds 2
}

$allResults | ConvertTo-Json -Depth 10 | Out-File -FilePath "apify_raw_data.json" -Encoding utf8
Write-Host "DONE! Saved to apify_raw_data.json"