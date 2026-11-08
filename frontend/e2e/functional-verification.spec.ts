import { test, expect } from '@playwright/test'

const PASSWORD = 'eeUf2oJkuQBT1ma9ngtNUfny1k6N'

async function login(page: any) {
  await page.goto('/login')
  await page.waitForSelector('input[type="password"]', { timeout: 60000 })
  await page.fill('input[type="password"]', PASSWORD)
  await page.click('button[type="submit"]')
  await page.waitForURL('**/notebooks', { timeout: 60000 })
}

async function selectNthDropdown(page: any, nth: number, optionText: string) {
  const triggers = page.locator('[data-slot="select-trigger"]')
  await triggers.nth(nth).click({ timeout: 15000 })
  await page.getByRole('option', { name: optionText }).click({ timeout: 15000 })
}

test.describe('Functional Verification — PC Build', () => {
  test('login and access notebooks', async ({ page }) => {
    await login(page)
    await expect(page).toHaveURL(/.*notebooks/)
    await expect(page.getByRole('heading', { name: 'Notebooks' })).toBeVisible()
  })

  test('assessments page loads and accepts input', async ({ page, request }) => {
    await login(page)

    await page.goto('/assessments')
    await page.waitForURL('**/assessments', { timeout: 30000 })
    await expect(page.getByRole('heading', { name: /assessment/i })).toBeVisible({ timeout: 30000 })

    // Wait for notebooks to load
    await page.waitForFunction(() => {
      const triggers = document.querySelectorAll('[data-slot="select-trigger"]')
      return triggers.length >= 3
    }, { timeout: 15000 })

    // Select notebook "test" (notebook:01lhg0esv4lfmfjfpif3)
    await selectNthDropdown(page, 0, 'test')
    await selectNthDropdown(page, 1, 'Biology')
    await selectNthDropdown(page, 2, 'Quiz')

    const scoreInput = page.locator('input[type="number"]').first()
    await scoreInput.fill('85')

    await page.click('button:has-text("Record Assessment")', { timeout: 10000 })

    // Wait for any feedback
    await page.waitForTimeout(2000)

    // Verify via API that the assessment was created with the correct notebook
    const apiResponse = await request.get('/api/assessments', {
      headers: { Authorization: `Bearer ${PASSWORD}` }
    })
    const assessments = await apiResponse.json()
    const latest = assessments[0]
    expect(latest.notebook_id).toBe('notebook:01lhg0esv4lfmfjfpif3')
    expect(latest.field).toBe('biology')
    expect(latest.score).toBe(85)
  })

  test('timetable page loads and accepts input', async ({ page }) => {
    await login(page)

    await page.goto('/timetable')
    await page.waitForURL('**/timetable', { timeout: 30000 })
    await expect(page.getByRole('heading', { name: /timetable/i })).toBeVisible({ timeout: 30000 })

    await page.fill('input[placeholder*="Biology Chapter" i]', 'Biology Chapter 3')

    await selectNthDropdown(page, 0, 'Study')
    await selectNthDropdown(page, 1, 'Biology')

    const timeInputs = page.locator('input[type="time"]')
    await timeInputs.first().fill('09:00')
    await timeInputs.last().fill('11:00')

    await page.click('button:has-text("Add Block")', { timeout: 10000 })
    await page.waitForTimeout(2000)

    await expect(page.locator('text=Biology Chapter 3').first()).toBeVisible({ timeout: 10000 })
  })

  test('dynamic course page loads and creates course', async ({ page }) => {
    await login(page)

    await page.goto('/dynamic-course')
    await page.waitForURL('**/dynamic-course', { timeout: 30000 })
    await expect(page.getByRole('heading', { name: /dynamic course/i })).toBeVisible({ timeout: 30000 })

    await page.fill('input[placeholder*="Quantum" i]', 'Biology Fundamentals')

    await selectNthDropdown(page, 0, 'Biology')

    await page.click('button:has-text("Create Course")', { timeout: 10000 })

    await expect(page.getByText('Biology Fundamentals', { exact: true }).first()).toBeVisible({ timeout: 15000 })
  })
})

test.describe('Backend API spot-checks — PC', () => {
  test('assessments API returns data', async ({ request }) => {
    const response = await request.get('/api/assessments', {
      headers: { Authorization: `Bearer ${PASSWORD}` }
    })
    expect(response.ok()).toBeTruthy()
  })

  test('timetable API returns data', async ({ request }) => {
    const response = await request.get('/api/timetable', {
      headers: { Authorization: `Bearer ${PASSWORD}` }
    })
    expect(response.ok()).toBeTruthy()
  })

  test('spiderweb metrics API returns data', async ({ request }) => {
    const response = await request.get('/api/assessments/spiderweb', {
      headers: { Authorization: `Bearer ${PASSWORD}` }
    })
    expect(response.ok()).toBeTruthy()
  })
})