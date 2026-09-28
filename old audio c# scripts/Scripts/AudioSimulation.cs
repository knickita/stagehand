using System;
using System.Collections;
using System.Collections.Generic;
using Unity.VisualScripting;
using UnityEngine;

[ExecuteAlways]
public class AudioSimulation : MonoBehaviour
{
    public float splAtOneMeter = 100.0f; // Sound pressure level of the direct sound at reference distance (dB SPL)
    public float distance = 10.0f;        // Distance from the source (in meters)    
    public float frequency;
    public float W,H;
    public float D1 = 2.0f;        // Dimension of the sound source in the first direction (in meters)
    public float D2 = 2.0f;        // Dimension of the sound source in the second direction (in meters)
    public float n = 1.0f;         // Directivity factor

    [Header("Result")]
    public float L_p=0;
    public float farFieldDistance=0;

    // Start is called before the first frame update
    void Start()
    {
        
    }

    // Update is called once per frame
    void Update()
    {
        // Calculate sound pressure level
        //L_p = splAtOneMeter - 20 * Mathf.Log10(r_0 / distance) + 10 * n * (Mathf.Log10(D1 / r_0) + Mathf.Log10(D2 / r_0));        
        L_p = splAtOneMeter - 20 * Mathf.Log10(1 / distance) + 10 * n * (Mathf.Log10(D1) + Mathf.Log10(D2));
        float wavelength = 343 / frequency;
        //farFieldDistance=2*(Mathf.Log(W) + Mathf.Log(H))/wavelength;
        //farFieldDistance = 2 * (W*H) / wavelength;
        farFieldDistance = (W*W/(2*wavelength))-(wavelength/8);
    }
}
