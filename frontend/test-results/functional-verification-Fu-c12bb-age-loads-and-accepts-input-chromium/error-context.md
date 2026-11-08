# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: functional-verification.spec.ts >> Functional Verification — PC Build >> assessments page loads and accepts input
- Location: e2e/functional-verification.spec.ts:26:3

# Error details

```
TimeoutError: page.waitForSelector: Timeout 60000ms exceeded.
Call log:
  - waiting for locator('input[type="password"]') to be visible

```

# Page snapshot

```yaml
- generic [active]:
  - alert [ref=e1]
  - alert [ref=e2]:
    - generic [ref=e3]:
      - generic [ref=e8]:
        - heading "Unable to Connect to API Server" [level=1] [ref=e9]
        - paragraph [ref=e10]: The Open Notebook API server could not be reached
      - generic [ref=e11]:
        - heading "This usually means:" [level=2] [ref=e12]
        - list [ref=e13]:
          - listitem [ref=e14]: The API server is not running
          - listitem [ref=e15]: The API server is running on a different address
          - listitem [ref=e16]: Network connectivity issues
        - heading "Quick fixes:" [level=2] [ref=e17]
        - generic [ref=e18]:
          - paragraph [ref=e19]: "Set the API_URL environment variable:"
          - code [ref=e20]: "# For Docker:docker run -e API_URL=http://your-host:5055 ...# For local development:API_URL=http://localhost:5055"
      - generic [ref=e21]:
        - paragraph [ref=e22]: "For detailed setup instructions, see:"
        - link "Open Notebook Documentation" [ref=e23] [cursor=pointer]:
          - /url: https://github.com/lfnovo/open-notebook
      - button "Show Technical Details" [ref=e29]
      - generic [ref=e31]:
        - button "Retry Connection" [ref=e32]
        - paragraph [ref=e33]: Press R or click the button to retry
```

# Test source

```ts
  1   | import { test, expect } from '@playwright/test'
  2   | 
  3   | const PASSWORD = 'eeUf2oJkuQBT1ma9ngtNUfny1k6N'
  4   | 
  5   | async function login(page: any) {
  6   |   await page.goto('/login')
> 7   |   await page.waitForSelector('input[type="password"]', { timeout: 60000 })
      |              ^ TimeoutError: page.waitForSelector: Timeout 60000ms exceeded.
  8   |   await page.fill('input[type="password"]', PASSWORD)
  9   |   await page.click('button[type="submit"]')
  10  |   await page.waitForURL('**/notebooks', { timeout: 60000 })
  11  | }
  12  | 
  13  | async function selectNthDropdown(page: any, nth: number, optionText: string) {
  14  |   const triggers = page.locator('[data-slot="select-trigger"]')
  15  |   await triggers.nth(nth).click({ timeout: 15000 })
  16  |   await page.getByRole('option', { name: optionText }).click({ timeout: 15000 })
  17  | }
  18  | 
  19  | test.describe('Functional Verification — PC Build', () => {
  20  |   test('login and access notebooks', async ({ page }) => {
  21  |     await login(page)
  22  |     await expect(page).toHaveURL(/.*notebooks/)
  23  |     await expect(page.getByRole('heading', { name: 'Notebooks' })).toBeVisible()
  24  |   })
  25  | 
  26  |   test('assessments page loads and accepts input', async ({ page, request }) => {
  27  |     await login(page)
  28  | 
  29  |     await page.goto('/assessments')
  30  |     await page.waitForURL('**/assessments', { timeout: 30000 })
  31  |     await expect(page.getByRole('heading', { name: /assessment/i })).toBeVisible({ timeout: 30000 })
  32  | 
  33  |     // Wait for notebooks to load
  34  |     await page.waitForFunction(() => {
  35  |       const triggers = document.querySelectorAll('[data-slot="select-trigger"]')
  36  |       return triggers.length >= 3
  37  |     }, { timeout: 15000 })
  38  | 
  39  |     // Select notebook "test" (notebook:01lhg0esv4lfmfjfpif3)
  40  |     await selectNthDropdown(page, 0, 'test')
  41  |     await selectNthDropdown(page, 1, 'Biology')
  42  |     await selectNthDropdown(page, 2, 'Quiz')
  43  | 
  44  |     const scoreInput = page.locator('input[type="number"]').first()
  45  |     await scoreInput.fill('85')
  46  | 
  47  |     await page.click('button:has-text("Record Assessment")', { timeout: 10000 })
  48  | 
  49  |     // Wait for any feedback
  50  |     await page.waitForTimeout(2000)
  51  | 
  52  |     // Verify via API that the assessment was created with the correct notebook
  53  |     const apiResponse = await request.get('/api/assessments', {
  54  |       headers: { Authorization: `Bearer ${PASSWORD}` }
  55  |     })
  56  |     const assessments = await apiResponse.json()
  57  |     const latest = assessments[0]
  58  |     expect(latest.notebook_id).toBe('notebook:01lhg0esv4lfmfjfpif3')
  59  |     expect(latest.field).toBe('biology')
  60  |     expect(latest.score).toBe(85)
  61  |   })
  62  | 
  63  |   test('timetable page loads and accepts input', async ({ page }) => {
  64  |     await login(page)
  65  | 
  66  |     await page.goto('/timetable')
  67  |     await page.waitForURL('**/timetable', { timeout: 30000 })
  68  |     await expect(page.getByRole('heading', { name: /timetable/i })).toBeVisible({ timeout: 30000 })
  69  | 
  70  |     await page.fill('input[placeholder*="Biology Chapter" i]', 'Biology Chapter 3')
  71  | 
  72  |     await selectNthDropdown(page, 0, 'Study')
  73  |     await selectNthDropdown(page, 1, 'Biology')
  74  | 
  75  |     const timeInputs = page.locator('input[type="time"]')
  76  |     await timeInputs.first().fill('09:00')
  77  |     await timeInputs.last().fill('11:00')
  78  | 
  79  |     await page.click('button:has-text("Add Block")', { timeout: 10000 })
  80  |     await page.waitForTimeout(2000)
  81  | 
  82  |     await expect(page.locator('text=Biology Chapter 3').first()).toBeVisible({ timeout: 10000 })
  83  |   })
  84  | 
  85  |   test('dynamic course page loads and creates course', async ({ page }) => {
  86  |     await login(page)
  87  | 
  88  |     await page.goto('/dynamic-course')
  89  |     await page.waitForURL('**/dynamic-course', { timeout: 30000 })
  90  |     await expect(page.getByRole('heading', { name: /dynamic course/i })).toBeVisible({ timeout: 30000 })
  91  | 
  92  |     await page.fill('input[placeholder*="Quantum" i]', 'Biology Fundamentals')
  93  | 
  94  |     await selectNthDropdown(page, 0, 'Biology')
  95  | 
  96  |     await page.click('button:has-text("Create Course")', { timeout: 10000 })
  97  | 
  98  |     await expect(page.getByText('Biology Fundamentals', { exact: true }).first()).toBeVisible({ timeout: 15000 })
  99  |   })
  100 | })
  101 | 
  102 | test.describe('Backend API spot-checks — PC', () => {
  103 |   test('assessments API returns data', async ({ request }) => {
  104 |     const response = await request.get('/api/assessments', {
  105 |       headers: { Authorization: `Bearer ${PASSWORD}` }
  106 |     })
  107 |     expect(response.ok()).toBeTruthy()
```